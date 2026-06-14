import { createHash, randomUUID } from "node:crypto";
import { InMemoryCheckpointStore, InMemoryEventSink } from "./checkpoint.js";
import { orderedSteps, validatePlan } from "./planValidation.js";
import type {
  AgentEvent,
  AgentModule,
  AgentPlan,
  AgentRunState,
  AgentStep,
  AgentTask,
  CheckpointStore,
  DurableRunStore,
  EventSink,
  JsonObject,
  PlannerModule,
  ReviewerModule,
} from "./types.js";

export interface AgentKernelOptions {
  planner: PlannerModule;
  reviewer?: ReviewerModule;
  agents: AgentModule[];
  checkpoints?: CheckpointStore;
  events?: EventSink;
  runStore?: DurableRunStore;
  retryPolicy?: StepRetryPolicy;
}

export interface StepRetryPolicy {
  maxAttempts: number;
  retryableErrorPatterns?: string[];
}

export interface ApprovalResumeRequest {
  runId: string;
  approvedPermissions: string[];
  approvedBy?: string;
  reason?: string;
}

export interface RepairResumeRequest {
  runId: string;
  inputPatch?: JsonObject;
  resetStepIds?: string[];
  repairedBy?: string;
  reason?: string;
}

export interface CancelRunRequest {
  runId: string;
  cancelledBy?: string;
  reason?: string;
}

export class AgentKernel {
  private readonly agents: Map<string, AgentModule>;
  private readonly checkpoints: CheckpointStore;
  private readonly events: EventSink;
  private readonly retryPolicy: StepRetryPolicy;

  constructor(private readonly options: AgentKernelOptions) {
    this.agents = new Map(options.agents.map((agent) => [agent.role, agent]));
    this.checkpoints = options.runStore ?? options.checkpoints ?? new InMemoryCheckpointStore();
    this.events = options.runStore ?? options.events ?? new InMemoryEventSink();
    this.retryPolicy = normalizeRetryPolicy(options.retryPolicy);
  }

  async run(task: AgentTask): Promise<AgentRunState> {
    const now = new Date().toISOString();
    const state: AgentRunState = {
      run_id: `run_${stableHash({ task_id: task.task_id, nonce: randomUUID() }).slice(0, 16)}`,
      task,
      status: "initialized",
      events: [],
      created_at: now,
      updated_at: now,
    };
    await this.emit(state, "run_started", { title: task.title });

    state.status = "planning";
    let plan: AgentPlan;
    try {
      plan = await this.options.planner.createPlan(task);
      validatePlan(plan, task);
    } catch (caught) {
      state.status = "failed";
      await this.emit(state, "plan_failed", { error: errorMessage(caught) });
      await this.emit(state, "run_failed", { reason: "plan_failed" });
      return state;
    }
    state.plan = plan;
    await this.emit(state, "plan_created", { plan_id: plan.plan_id, steps: plan.steps.length });

    return this.executeAndReview(state, plan);
  }

  async resume(runId: string): Promise<AgentRunState | undefined> {
    return this.checkpoints.load(runId);
  }

  async approveAndResume(request: ApprovalResumeRequest): Promise<AgentRunState | undefined> {
    if (request.approvedPermissions.length === 0) throw new Error("At least one approved permission is required.");
    const state = await this.checkpoints.load(request.runId);
    if (!state) return undefined;
    if (!state.plan) throw new Error(`Run ${request.runId} has no plan to resume.`);
    if (state.status !== "waiting_for_approval") {
      throw new Error(`Run ${request.runId} is ${state.status}; expected waiting_for_approval.`);
    }
    const approvalStepIds = state.plan.steps.filter((step) => containsApprovalRequired(step.output)).map((step) => step.step_id);
    if (approvalStepIds.length === 0) throw new Error(`Run ${request.runId} has no approval-gated steps to resume.`);

    state.task.input.approved_permissions = uniqueStrings([
      ...stringArray(state.task.input.approved_permissions),
      ...request.approvedPermissions,
    ]);
    const resetStepIds = resetStepsForReexecution(state.plan, approvalStepIds);
    for (const step of state.plan.steps) step.input.approved_permissions = state.task.input.approved_permissions;
    delete state.review;
    await this.emit(state, "approval_granted", {
      approved_permissions: request.approvedPermissions,
      approved_by: request.approvedBy ?? "human",
      reason: request.reason ?? "approval granted",
      reset_steps: resetStepIds,
    });
    await this.emit(state, "run_resumed", { from_status: "waiting_for_approval", reset_steps: resetStepIds });
    return this.executeAndReview(state, state.plan, { skipSucceeded: true });
  }

  async repairAndResume(request: RepairResumeRequest): Promise<AgentRunState | undefined> {
    const state = await this.checkpoints.load(request.runId);
    if (!state) return undefined;
    if (!state.plan) throw new Error(`Run ${request.runId} has no plan to repair.`);
    if (state.status !== "repairing") {
      throw new Error(`Run ${request.runId} is ${state.status}; expected repairing.`);
    }
    const failedStepIds = state.plan.steps.filter((step) => step.status === "failed").map((step) => step.step_id);
    const rootStepIds = request.resetStepIds ?? failedStepIds;
    if (rootStepIds.length === 0) throw new Error(`Run ${request.runId} has no failed steps to repair.`);
    if (request.inputPatch) {
      state.task.input = { ...state.task.input, ...request.inputPatch };
      for (const step of state.plan.steps) step.input = { ...step.input, ...request.inputPatch };
    }
    const resetStepIds = resetStepsForReexecution(state.plan, rootStepIds);
    delete state.review;
    await this.emit(state, "repair_applied", {
      repaired_by: request.repairedBy ?? "human",
      reason: request.reason ?? "repair applied",
      reset_steps: resetStepIds,
      input_patch_keys: Object.keys(request.inputPatch ?? {}).sort(),
    });
    await this.emit(state, "run_resumed", { from_status: "repairing", reset_steps: resetStepIds });
    return this.executeAndReview(state, state.plan, { skipSucceeded: true });
  }

  async cancelRun(request: CancelRunRequest): Promise<AgentRunState | undefined> {
    const state = await this.checkpoints.load(request.runId);
    if (!state) return undefined;
    if (isTerminalStatus(state.status)) {
      throw new Error(`Run ${request.runId} is already terminal: ${state.status}.`);
    }
    const previousStatus = state.status;
    state.status = "cancelled";
    await this.emit(state, "run_cancelled", {
      from_status: previousStatus,
      cancelled_by: request.cancelledBy ?? "human",
      reason: request.reason ?? "run cancelled",
    });
    return state;
  }

  private async executeAndReview(
    state: AgentRunState,
    plan: AgentPlan,
    options: { skipSucceeded?: boolean } = {},
  ): Promise<AgentRunState> {
    state.status = "running";
    for (const step of orderedSteps(plan)) {
      if (options.skipSucceeded && step.status === "succeeded") continue;
      const dependencyFailure = step.depends_on.some((id) => plan.steps.find((candidate) => candidate.step_id === id)?.status === "failed");
      if (dependencyFailure) {
        step.status = "skipped";
        await this.emit(state, "step_skipped", { step_id: step.step_id, reason: "dependency_failed" }, step);
        continue;
      }
      await this.runStep(state, step);
      if (step.status === "failed") break;
    }

    if (plan.steps.some((step) => step.status === "failed") && !this.options.reviewer) {
      state.status = "failed";
      await this.emit(state, "run_failed", { reason: "step_failed" });
      return state;
    }

    return this.reviewAndFinalize(state);
  }

  private async reviewAndFinalize(state: AgentRunState): Promise<AgentRunState> {
    if (this.options.reviewer) {
      state.status = "reviewing";
      await this.emit(state, "review_started", { reviewer: this.options.reviewer.constructor.name });
      try {
        state.review = await this.options.reviewer.review(state);
      } catch (caught) {
        state.status = "failed";
        await this.emit(state, "review_failed", { error: errorMessage(caught) });
        await this.emit(state, "run_failed", { reason: "review_failed" });
        return state;
      }
      await this.emit(state, "review_completed", { status: state.review.status, findings: state.review.findings.length });
      if (state.review.status === "repair") {
        state.status = "repairing";
        await this.emit(state, "repair_requested", { reason: state.review.summary });
        return state;
      }
      if (state.review.status === "needs_approval") {
        state.status = "waiting_for_approval";
        await this.emit(state, "approval_required", { reason: state.review.summary });
        return state;
      }
      if (state.review.status === "rejected") {
        state.status = "failed";
        await this.emit(state, "run_failed", { reason: state.review.summary });
        return state;
      }
    }

    state.status = "completed";
    await this.emit(state, "run_completed", { status: state.status });
    return state;
  }

  private async runStep(state: AgentRunState, step: AgentStep): Promise<void> {
    for (let attempt = 1; attempt <= this.retryPolicy.maxAttempts; attempt += 1) {
      const agent = this.agents.get(step.agent_role);
      step.status = "running";
      await this.emit(state, "step_started", { step_id: step.step_id, attempt, max_attempts: this.retryPolicy.maxAttempts }, step);
      try {
        if (!agent) throw new Error(`No agent registered for role ${step.agent_role}`);
        step.output = await agent.runStep(step, state, {
          emit: (type, payload, eventStep = step) => this.emit(state, type, payload, eventStep),
        });
        step.status = "succeeded";
        delete step.error;
        await this.emit(state, "step_completed", { step_id: step.step_id, attempt }, step);
        return;
      } catch (caught) {
        step.error = errorMessage(caught);
        step.status = "failed";
        const retryable = attempt < this.retryPolicy.maxAttempts && isRetryable(step.error, this.retryPolicy);
        await this.emit(state, "step_failed", { step_id: step.step_id, error: step.error, attempt, retryable }, step);
        if (!retryable) return;
        await this.emit(
          state,
          "repair_requested",
          { step_id: step.step_id, reason: "retrying_failed_step", attempt, next_attempt: attempt + 1 },
          step,
        );
      }
    }
  }

  private async emit(state: AgentRunState, type: AgentEvent["type"], payload: JsonObject, step?: AgentStep): Promise<void> {
    const event: AgentEvent = {
      run_id: state.run_id,
      task_id: state.task.task_id,
      event_id: `evt_${state.events.length + 1}_${randomUUID().slice(0, 8)}`,
      sequence: state.events.length + 1,
      timestamp: new Date().toISOString(),
      type,
      ...(step ? { agent_role: step.agent_role, step_id: step.step_id } : {}),
      payload,
    };
    state.events.push(event);
    state.updated_at = event.timestamp;
    if (isDurableRunStore(this.events)) {
      await this.events.appendAndCheckpoint(event, state);
    } else {
      await this.events.append(event);
      await this.checkpoints.save(state);
    }
  }
}

function isDurableRunStore(value: EventSink): value is DurableRunStore {
  return "appendAndCheckpoint" in value && typeof value.appendAndCheckpoint === "function";
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

function normalizeRetryPolicy(policy: StepRetryPolicy | undefined): StepRetryPolicy {
  return {
    maxAttempts: Math.max(1, Math.floor(policy?.maxAttempts ?? 1)),
    ...(policy?.retryableErrorPatterns ? { retryableErrorPatterns: policy.retryableErrorPatterns } : {}),
  };
}

function isRetryable(error: string, policy: StepRetryPolicy): boolean {
  if (!policy.retryableErrorPatterns || policy.retryableErrorPatterns.length === 0) return true;
  return policy.retryableErrorPatterns.some((pattern) => error.includes(pattern));
}

function containsApprovalRequired(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(containsApprovalRequired);
  const record = value as Record<string, unknown>;
  if (record.approval_required === true) return true;
  return Object.values(record).some(containsApprovalRequired);
}

function resetStepsForReexecution(plan: AgentPlan, rootStepIds: string[]): string[] {
  const resetIds = new Set(rootStepIds);
  let changed = true;
  while (changed) {
    changed = false;
    for (const step of plan.steps) {
      if (!resetIds.has(step.step_id) && step.depends_on.some((dependency) => resetIds.has(dependency))) {
        resetIds.add(step.step_id);
        changed = true;
      }
    }
  }
  for (const step of plan.steps) {
    if (!resetIds.has(step.step_id)) continue;
    step.status = "pending";
    delete step.output;
    delete step.error;
  }
  return [...resetIds].sort();
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

function isTerminalStatus(status: AgentRunState["status"]): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function stableHash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
