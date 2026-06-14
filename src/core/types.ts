export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = Record<string, unknown>;

export type AgentRunStatus =
  | "initialized"
  | "planning"
  | "running"
  | "waiting_for_tool"
  | "reviewing"
  | "repairing"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type AgentRole =
  | "supervisor"
  | "planner"
  | "executor"
  | "reviewer"
  | "repair"
  | "memory"
  | "domain_adapter"
  | string;

export type StepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

export interface AgentTask {
  task_id: string;
  title: string;
  objective: string;
  input: JsonObject;
  domain?: string;
  priority?: "low" | "normal" | "high";
  constraints?: string[];
  success_criteria?: string[];
}

export interface AgentStep {
  step_id: string;
  agent_role: AgentRole;
  title: string;
  status: StepStatus;
  depends_on: string[];
  skill_refs: string[];
  input: JsonObject;
  output?: JsonObject;
  error?: string;
}

export type PlanSource = "static" | "model" | "fallback";

export interface AgentPlanMetadata {
  source: PlanSource;
  planner_name: string;
  prompt_version?: string;
  provider?: string;
  model?: string;
  latency_ms?: number;
  warnings?: string[];
}

export interface AgentPlan {
  plan_id: string;
  task_id: string;
  strategy: string;
  steps: AgentStep[];
  metadata?: AgentPlanMetadata;
}

export interface AgentEvent {
  run_id: string;
  task_id: string;
  event_id: string;
  sequence: number;
  timestamp: string;
  type:
    | "run_started"
    | "plan_created"
    | "plan_failed"
    | "step_started"
    | "step_skipped"
    | "skill_invoked"
    | "skill_completed"
    | "skill_failed"
    | "step_completed"
    | "step_failed"
    | "review_started"
    | "review_completed"
    | "review_failed"
    | "repair_applied"
    | "repair_requested"
    | "approval_required"
    | "approval_granted"
    | "run_resumed"
    | "run_completed"
    | "run_cancelled"
    | "run_failed";
  agent_role?: AgentRole;
  step_id?: string;
  payload: JsonObject;
}

export interface ReviewFinding {
  finding_id: string;
  severity: "info" | "warning" | "error" | "critical";
  category:
    | "unsupported_output"
    | "tool_failure"
    | "unsafe_side_effect"
    | "missing_context"
    | "schema_violation"
    | "budget_exceeded"
    | string;
  message: string;
  evidence_refs: string[];
}

export interface ReviewReport {
  status: "accepted" | "repair" | "needs_approval" | "rejected";
  findings: ReviewFinding[];
  summary: string;
}

export interface AgentRunState {
  run_id: string;
  task: AgentTask;
  status: AgentRunStatus;
  plan?: AgentPlan;
  events: AgentEvent[];
  review?: ReviewReport;
  created_at: string;
  updated_at: string;
}

export interface AgentModule {
  role: AgentRole;
  runStep(step: AgentStep, state: AgentRunState, context?: StepRuntimeContext): Promise<JsonObject>;
}

export interface StepRuntimeContext {
  emit(type: AgentEvent["type"], payload: JsonObject, step?: AgentStep): Promise<void>;
}

export interface PlannerModule {
  createPlan(task: AgentTask): Promise<AgentPlan>;
}

export interface ReviewerModule {
  review(state: AgentRunState): Promise<ReviewReport>;
}

export interface CheckpointStore {
  save(state: AgentRunState): Promise<void>;
  load(runId: string): Promise<AgentRunState | undefined>;
}

export interface EventSink {
  append(event: AgentEvent): Promise<void>;
}

export interface DurableRunStore extends CheckpointStore, EventSink {
  appendAndCheckpoint(event: AgentEvent, state: AgentRunState): Promise<void>;
  listEvents?(runId: string): Promise<AgentEvent[]>;
  listRuns?(): Promise<AgentRunState[]>;
}
