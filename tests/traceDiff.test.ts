import assert from "node:assert/strict";
import test from "node:test";
import { diffTraceEvents, type AgentEvent } from "../src/index.js";

test("trace diff reports added tool events and stable terminal status", () => {
  const left = [event("run_a", 1, "run_started"), event("run_a", 2, "run_completed")];
  const right = [
    event("run_b", 1, "run_started"),
    event("run_b", 2, "skill_invoked"),
    event("run_b", 3, "run_completed"),
  ];

  const diff = diffTraceEvents(left, right);

  assert.equal(diff.left_run_id, "run_a");
  assert.equal(diff.right_run_id, "run_b");
  assert.equal(diff.same_terminal_status, true);
  assert.deepEqual(diff.event_type_added, ["skill_invoked"]);
  assert.equal(diff.skill_invocation_delta, 1);
  assert.equal(diff.failure_delta, 0);
  assert.equal(diff.sequence_length_delta, 1);
});

test("trace diff reports terminal status drift and failure delta", () => {
  const left = [event("run_a", 1, "run_started"), event("run_a", 2, "run_completed")];
  const right = [event("run_b", 1, "run_started"), event("run_b", 2, "run_failed")];

  const diff = diffTraceEvents(left, right);

  assert.equal(diff.same_terminal_status, false);
  assert.equal(diff.left_terminal_event, "run_completed");
  assert.equal(diff.right_terminal_event, "run_failed");
  assert.deepEqual(diff.event_type_added, ["run_failed"]);
  assert.deepEqual(diff.event_type_removed, ["run_completed"]);
  assert.equal(diff.failure_delta, 1);
});

test("trace diff treats cancellation as terminal status drift", () => {
  const left = [event("run_a", 1, "run_started"), event("run_a", 2, "run_completed")];
  const right = [event("run_b", 1, "run_started"), event("run_b", 2, "run_cancelled")];

  const diff = diffTraceEvents(left, right);

  assert.equal(diff.same_terminal_status, false);
  assert.equal(diff.right_terminal_event, "run_cancelled");
  assert.deepEqual(diff.event_type_added, ["run_cancelled"]);
});

function event(runId: string, sequence: number, type: AgentEvent["type"]): AgentEvent {
  return {
    run_id: runId,
    task_id: "task_1",
    event_id: `${runId}_${sequence}`,
    sequence,
    timestamp: "2026-05-31T00:00:00.000Z",
    type,
    payload: {},
  };
}
