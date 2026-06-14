import { createHash } from "node:crypto";
import type { AgentEvent } from "../core/types.js";
import { redactForTrace, type RedactionOptions } from "./redaction.js";
import type { TraceArtifactRef, TraceSpan, TraceSummary } from "./types.js";

export interface TraceExportOptions {
  redaction?: RedactionOptions;
}

export function exportTrace(events: AgentEvent[], options: TraceExportOptions = {}): TraceArtifactRef {
  if (events.length === 0) {
    return {
      run_id: "",
      task_id: "",
      events: [],
      spans: [],
      summary: summarizeTraceEvents([]),
    };
  }
  const runId = events[0]?.run_id ?? "";
  const taskId = events[0]?.task_id ?? "";
  return {
    run_id: runId,
    task_id: taskId,
    events,
    spans: events.map((event) => eventToSpan(runId, event, options)),
    summary: summarizeTraceEvents(events),
  };
}

export function summarizeTraceEvents(events: AgentEvent[]): TraceSummary {
  const terminal = terminalEvent(events);
  return {
    status: traceStatus(terminal?.type),
    ...(terminal ? { terminal_event: terminal.type } : {}),
    event_count: events.length,
    duration_ms: traceDurationMs(events),
    skill_invocations: countEvents(events, "skill_invoked"),
    skill_failures: countEvents(events, "skill_failed"),
    approval_required: countEvents(events, "approval_required"),
    approval_granted: countEvents(events, "approval_granted"),
    repair_requested: countEvents(events, "repair_requested"),
    repair_applied: countEvents(events, "repair_applied"),
  };
}

export function eventToSpan(traceId: string, event: AgentEvent, options: TraceExportOptions = {}): TraceSpan {
  const payload = redactForTrace(event.payload, options.redaction);
  return {
    trace_id: traceId,
    span_id: spanId(event),
    ...(event.step_id ? { parent_span_id: `step:${event.step_id}` } : {}),
    name: event.type,
    started_at: event.timestamp,
    ended_at: event.timestamp,
    status: event.type.endsWith("failed") || event.type === "run_failed" ? "error" : "ok",
    attributes: {
      event_id: event.event_id,
      sequence: event.sequence,
      agent_role: event.agent_role ?? "",
      step_id: event.step_id ?? "",
      payload_hash: hashJson(payload),
      payload,
    },
  };
}

function spanId(event: AgentEvent): string {
  return `${event.sequence}_${hashJson({ event_id: event.event_id, type: event.type }).slice(0, 12)}`;
}

function terminalEvent(events: AgentEvent[]): AgentEvent | undefined {
  return [...events]
    .reverse()
    .find((event) => event.type === "run_completed" || event.type === "run_failed" || event.type === "run_cancelled");
}

function traceStatus(type: AgentEvent["type"] | undefined): TraceSummary["status"] {
  if (type === "run_completed") return "completed";
  if (type === "run_failed") return "failed";
  if (type === "run_cancelled") return "cancelled";
  return "nonterminal";
}

function traceDurationMs(events: AgentEvent[]): number {
  const first = events[0]?.timestamp;
  const last = events.at(-1)?.timestamp;
  if (!first || !last) return 0;
  const duration = Date.parse(last) - Date.parse(first);
  return Number.isFinite(duration) ? Math.max(0, duration) : 0;
}

function countEvents(events: AgentEvent[], type: AgentEvent["type"]): number {
  return events.filter((event) => event.type === type).length;
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
