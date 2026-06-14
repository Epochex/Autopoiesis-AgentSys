import type { AgentEvent } from "../core/types.js";

export interface TraceDiffResult {
  left_run_id: string;
  right_run_id: string;
  same_terminal_status: boolean;
  left_terminal_event?: string;
  right_terminal_event?: string;
  event_type_added: string[];
  event_type_removed: string[];
  skill_invocation_delta: number;
  failure_delta: number;
  sequence_length_delta: number;
}

export function diffTraceEvents(left: AgentEvent[], right: AgentEvent[]): TraceDiffResult {
  const leftTypes = eventTypeCounts(left);
  const rightTypes = eventTypeCounts(right);
  const leftTerminal = terminalEvent(left);
  const rightTerminal = terminalEvent(right);
  return {
    left_run_id: left[0]?.run_id ?? "",
    right_run_id: right[0]?.run_id ?? "",
    same_terminal_status: leftTerminal === rightTerminal,
    ...(leftTerminal ? { left_terminal_event: leftTerminal } : {}),
    ...(rightTerminal ? { right_terminal_event: rightTerminal } : {}),
    event_type_added: [...rightTypes.keys()].filter((type) => !leftTypes.has(type)).sort(),
    event_type_removed: [...leftTypes.keys()].filter((type) => !rightTypes.has(type)).sort(),
    skill_invocation_delta: (rightTypes.get("skill_invoked") ?? 0) - (leftTypes.get("skill_invoked") ?? 0),
    failure_delta: failureCount(rightTypes) - failureCount(leftTypes),
    sequence_length_delta: right.length - left.length,
  };
}

function eventTypeCounts(events: AgentEvent[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const event of events) counts.set(event.type, (counts.get(event.type) ?? 0) + 1);
  return counts;
}

function terminalEvent(events: AgentEvent[]): string | undefined {
  return [...events]
    .reverse()
    .find((event) => event.type === "run_completed" || event.type === "run_failed" || event.type === "run_cancelled")?.type;
}

function failureCount(counts: Map<string, number>): number {
  return [...counts.entries()].filter(([type]) => type.endsWith("_failed") || type === "run_failed").reduce((total, [, count]) => total + count, 0);
}
