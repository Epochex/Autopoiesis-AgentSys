import assert from "node:assert/strict";
import test from "node:test";
import { evaluateMatrixRegression, type MatrixHarnessReport } from "../src/index.js";

test("matrix regression gate accepts reports that satisfy policy thresholds", () => {
  const result = evaluateMatrixRegression(report(), {
    min_completion_rate: 1,
    max_failed_runs: 0,
    max_approval_required: 0,
    max_step_failures: 0,
    max_repair_requests: 0,
    max_avg_duration_ms: 10,
    require_profiles: ["static_default"],
    require_scenarios: ["office_launch_handoff"],
  });

  assert.equal(result.accepted, true);
  assert.equal(result.completion_rate, 1);
  assert.deepEqual(result.findings, []);
});

test("matrix regression gate reports coverage and metric drift", () => {
  const result = evaluateMatrixRegression(report({ failed: 1, completed: 0, avgDuration: 25 }), {
    min_completion_rate: 0.9,
    max_failed_runs: 0,
    max_step_failures: 0,
    max_repair_requests: 0,
    max_avg_duration_ms: 10,
    require_profiles: ["model_planner_default"],
    require_scenarios: ["decision_growth_case"],
  });

  assert.equal(result.accepted, false);
  assert.ok(result.findings.some((finding) => finding.check_id === "min_completion_rate"));
  assert.ok(result.findings.some((finding) => finding.check_id === "max_failed_runs"));
  assert.ok(result.findings.some((finding) => finding.check_id === "max_step_failures"));
  assert.ok(result.findings.some((finding) => finding.check_id === "max_repair_requests"));
  assert.ok(result.findings.some((finding) => finding.check_id === "max_avg_duration_ms"));
  assert.ok(result.findings.some((finding) => finding.check_id === "require_profiles"));
  assert.ok(result.findings.some((finding) => finding.check_id === "require_scenarios"));
});

function report(
  overrides: {
    completed?: number;
    failed?: number;
    avgDuration?: number;
  } = {},
): MatrixHarnessReport {
  const completed = overrides.completed ?? 1;
  const failed = overrides.failed ?? 0;
  const avgDuration = overrides.avgDuration ?? 4;
  return {
    started_at: "2026-05-31T00:00:00.000Z",
    ended_at: "2026-05-31T00:00:01.000Z",
    suite_id: "portable_work_items",
    rows: [
      {
        case_id: "office_launch_handoff",
        run_id: "run_1",
        status: failed > 0 ? "failed" : "completed",
        event_count: 10,
        plan_source: "static",
        step_count: 2,
        step_failures: failed,
        repair_requests: failed,
        skill_invocations: 1,
        skill_failures: failed,
        approval_events: 0,
        duration_ms: avgDuration,
        suite_id: "portable_work_items",
        scenario_id: "office_launch_handoff",
        profile_id: "static_default",
        profile_label: "Static default kernel",
        tags: ["static", "office"],
      },
    ],
    aggregate: {
      profiles: 1,
      scenarios: 1,
      runs: 1,
      completed,
      failed,
      approval_required: 0,
      step_failures: failed,
      repair_requests: failed,
    },
    by_profile: {
      static_default: {
        runs: 1,
        completed,
        failed,
        approval_required: 0,
        step_failures: failed,
        repair_requests: failed,
        avg_duration_ms: avgDuration,
      },
    },
  };
}
