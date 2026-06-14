import type { AgentEvent, JsonObject } from "../core/types.js";

export interface TraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id?: string;
  name: string;
  started_at: string;
  ended_at: string;
  status: "ok" | "error" | "pending";
  attributes: JsonObject;
}

export interface TraceArtifactRef {
  run_id: string;
  task_id: string;
  events: AgentEvent[];
  spans: TraceSpan[];
  summary: TraceSummary;
}

export interface TraceSummary {
  status: "completed" | "failed" | "cancelled" | "nonterminal";
  terminal_event?: AgentEvent["type"];
  event_count: number;
  duration_ms: number;
  skill_invocations: number;
  skill_failures: number;
  approval_required: number;
  approval_granted: number;
  repair_requested: number;
  repair_applied: number;
}
