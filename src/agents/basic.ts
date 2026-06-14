import type { AgentModule, AgentPlan, AgentRunState, AgentStep, AgentTask, JsonObject, PlannerModule, ReviewerModule, ReviewReport } from "../core/types.js";
import { SkillRegistry } from "../skills/registry.js";

export class StaticPlanner implements PlannerModule {
  async createPlan(task: AgentTask): Promise<AgentPlan> {
    return {
      plan_id: `plan_${task.task_id}`,
      task_id: task.task_id,
      strategy: "Create a minimal executable task graph from the task contract.",
      metadata: {
        source: "static",
        planner_name: "StaticPlanner",
      },
      steps: [
        {
          step_id: "step_plan_context",
          agent_role: "executor",
          title: "Prepare bounded execution context",
          status: "pending",
          depends_on: [],
          skill_refs: [],
          input: { objective: task.objective, domain: task.domain ?? "general" },
        },
        {
          step_id: "step_execute_primary",
          agent_role: "executor",
          title: "Execute primary task action",
          status: "pending",
          depends_on: ["step_plan_context"],
          skill_refs: Array.isArray(task.input.skill_refs) ? task.input.skill_refs.map(String) : [],
          input: task.input,
        },
      ],
    };
  }
}

export class SkillExecutorAgent implements AgentModule {
  readonly role = "executor";

  constructor(private readonly skills: SkillRegistry) {}

  async runStep(step: AgentStep, state: AgentRunState, context?: Parameters<AgentModule["runStep"]>[2]): Promise<JsonObject> {
    if (step.skill_refs.length === 0) {
      return {
        step_id: step.step_id,
        status: "ok",
        note: "No skill requested; context boundary prepared.",
      };
    }
    const skillOutputs: JsonObject[] = [];
    for (const skillName of step.skill_refs) {
      const invocation = {
        invocation_id: `${state.run_id}:${step.step_id}:${skillName}`,
        skill_name: skillName,
        input: step.input,
        context: {
          run_id: state.run_id,
          task_id: state.task.task_id,
          allowed_resource_refs: [],
          memory_refs: [],
          metadata: {
            step_id: step.step_id,
            approved_permissions: Array.isArray(state.task.input.approved_permissions) ? state.task.input.approved_permissions : [],
          },
        },
      };
      await context?.emit("skill_invoked", { skill_name: skillName, invocation_id: invocation.invocation_id }, step);
      const result = await this.skills.invoke(invocation);
      if (result.status === "approval_required") {
        await context?.emit("approval_required", { skill_name: skillName, invocation_id: invocation.invocation_id, error: result.error ?? "" }, step);
        skillOutputs.push({
          skill_name: skillName,
          output: result.output,
          observations: result.observations,
        });
        return {
          step_id: step.step_id,
          status: "approval_required",
          approval_required: true,
          skill_outputs: skillOutputs,
        };
      }
      if (result.status !== "ok") {
        await context?.emit("skill_failed", { skill_name: skillName, invocation_id: invocation.invocation_id, status: result.status, error: result.error ?? "" }, step);
        throw new Error(result.error ?? `Skill ${skillName} returned ${result.status}`);
      }
      await context?.emit("skill_completed", { skill_name: skillName, invocation_id: invocation.invocation_id, observations: result.observations.length }, step);
      skillOutputs.push({
        skill_name: skillName,
        output: result.output,
        observations: result.observations,
      });
    }
    return {
      step_id: step.step_id,
      status: "ok",
      skill_outputs: skillOutputs,
    };
  }
}

export class ContractReviewer implements ReviewerModule {
  async review(state: AgentRunState): Promise<ReviewReport> {
    const failedSteps = state.plan?.steps.filter((step) => step.status === "failed") ?? [];
    if (failedSteps.length > 0) {
      return {
        status: "repair",
        summary: `${failedSteps.length} step(s) failed and require repair.`,
        findings: failedSteps.map((step, index) => ({
          finding_id: `finding_${index + 1}`,
          severity: "error",
          category: "tool_failure",
          message: step.error ?? `Step ${step.step_id} failed.`,
          evidence_refs: [step.step_id],
        })),
      };
    }
    const approvalSteps = state.plan?.steps.filter((step) => containsApprovalRequired(step.output)) ?? [];
    if (approvalSteps.length > 0) {
      return {
        status: "needs_approval",
        summary: `${approvalSteps.length} step(s) require human approval before continuing.`,
        findings: approvalSteps.map((step, index) => ({
          finding_id: `approval_${index + 1}`,
          severity: "warning",
          category: "unsafe_side_effect",
          message: `Step ${step.step_id} requested approval.`,
          evidence_refs: [step.step_id],
        })),
      };
    }
    return {
      status: "accepted",
      summary: "All planned steps completed under the current contract.",
      findings: [],
    };
  }
}

function containsApprovalRequired(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(containsApprovalRequired);
  const record = value as Record<string, unknown>;
  if (record.approval_required === true) return true;
  return Object.values(record).some(containsApprovalRequired);
}
