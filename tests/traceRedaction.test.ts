import assert from "node:assert/strict";
import test from "node:test";
import { exportTrace, redactForTrace } from "../src/index.js";

test("trace redaction masks secret-like keys recursively", () => {
  const redacted = redactForTrace({
    apiKey: "abc",
    nested: {
      password: "pw",
      safe: "visible",
    },
  }) as Record<string, unknown>;

  assert.equal(redacted.apiKey, "[REDACTED]");
  assert.deepEqual(redacted.nested, { password: "[REDACTED]", safe: "visible" });
});

test("trace export redacts payload before span attributes are emitted", () => {
  const trace = exportTrace([
    {
      run_id: "run_1",
      task_id: "task_1",
      event_id: "evt_1",
      sequence: 1,
      timestamp: "2026-05-31T00:00:00.000Z",
      type: "run_started",
      payload: {
        token: "secret-token",
        title: "safe",
      },
    },
  ]);

  assert.equal((trace.spans[0]?.attributes.payload as Record<string, unknown>).token, "[REDACTED]");
  assert.equal((trace.spans[0]?.attributes.payload as Record<string, unknown>).title, "safe");
  assert.equal(trace.summary.status, "nonterminal");
  assert.equal(trace.summary.event_count, 1);
});

test("trace export includes run lifecycle summary metrics", () => {
  const trace = exportTrace([
    event(1, "run_started", "2026-05-31T00:00:00.000Z"),
    event(2, "skill_invoked", "2026-05-31T00:00:01.000Z"),
    event(3, "approval_required", "2026-05-31T00:00:02.000Z"),
    event(4, "approval_granted", "2026-05-31T00:00:03.000Z"),
    event(5, "repair_requested", "2026-05-31T00:00:04.000Z"),
    event(6, "repair_applied", "2026-05-31T00:00:05.000Z"),
    event(7, "run_completed", "2026-05-31T00:00:06.000Z"),
  ]);

  assert.equal(trace.summary.status, "completed");
  assert.equal(trace.summary.terminal_event, "run_completed");
  assert.equal(trace.summary.duration_ms, 6000);
  assert.equal(trace.summary.skill_invocations, 1);
  assert.equal(trace.summary.approval_required, 1);
  assert.equal(trace.summary.approval_granted, 1);
  assert.equal(trace.summary.repair_requested, 1);
  assert.equal(trace.summary.repair_applied, 1);
});

function event(sequence: number, type: Parameters<typeof exportTrace>[0][number]["type"], timestamp: string) {
  return {
    run_id: "run_1",
    task_id: "task_1",
    event_id: `evt_${sequence}`,
    sequence,
    timestamp,
    type,
    payload: {},
  };
}
