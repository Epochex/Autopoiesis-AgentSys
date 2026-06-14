import type { AgentTask, JsonObject } from "../core/types.js";

export interface HarnessCase {
  case_id: string;
  task: AgentTask;
  expected?: JsonObject;
  tags?: string[];
}

export interface HarnessRow {
  case_id: string;
  run_id: string;
  status: string;
  event_count: number;
  plan_source?: string;
  step_count: number;
  step_failures: number;
  repair_requests: number;
  skill_invocations: number;
  skill_failures: number;
  approval_events: number;
  duration_ms: number;
  review_status?: string;
}

export interface HarnessReport {
  started_at: string;
  ended_at: string;
  rows: HarnessRow[];
  aggregate: {
    cases: number;
    completed: number;
    failed: number;
    repair_requested: number;
    approval_required: number;
    step_failures: number;
    repair_requests: number;
    skill_invocations: number;
    skill_failures: number;
    approval_events: number;
  };
}
