import type { MatrixHarnessReport } from "./matrix.js";

export interface MatrixRegressionPolicy {
  min_completion_rate?: number;
  max_failed_runs?: number;
  max_approval_required?: number;
  max_step_failures?: number;
  max_repair_requests?: number;
  max_avg_duration_ms?: number;
  require_profiles?: string[];
  require_scenarios?: string[];
}

export interface MatrixRegressionFinding {
  check_id: string;
  path: string;
  message: string;
  expected: string | number | string[];
  actual: string | number | string[];
}

export interface MatrixRegressionReport {
  accepted: boolean;
  suite_id: string;
  completion_rate: number;
  findings: MatrixRegressionFinding[];
}

export function evaluateMatrixRegression(
  report: MatrixHarnessReport,
  policy: MatrixRegressionPolicy,
): MatrixRegressionReport {
  const findings: MatrixRegressionFinding[] = [];
  const completionRate = report.aggregate.runs > 0 ? report.aggregate.completed / report.aggregate.runs : 0;

  if (policy.min_completion_rate !== undefined && completionRate < policy.min_completion_rate) {
    findings.push({
      check_id: "min_completion_rate",
      path: "$.aggregate.completed",
      message: "Completion rate is below the configured minimum.",
      expected: policy.min_completion_rate,
      actual: roundRate(completionRate),
    });
  }
  if (policy.max_failed_runs !== undefined && report.aggregate.failed > policy.max_failed_runs) {
    findings.push({
      check_id: "max_failed_runs",
      path: "$.aggregate.failed",
      message: "Failed run count exceeds the configured maximum.",
      expected: policy.max_failed_runs,
      actual: report.aggregate.failed,
    });
  }
  if (
    policy.max_approval_required !== undefined &&
    report.aggregate.approval_required > policy.max_approval_required
  ) {
    findings.push({
      check_id: "max_approval_required",
      path: "$.aggregate.approval_required",
      message: "Approval-required run count exceeds the configured maximum.",
      expected: policy.max_approval_required,
      actual: report.aggregate.approval_required,
    });
  }
  if (policy.max_step_failures !== undefined && report.aggregate.step_failures > policy.max_step_failures) {
    findings.push({
      check_id: "max_step_failures",
      path: "$.aggregate.step_failures",
      message: "Step failure events exceed the configured maximum.",
      expected: policy.max_step_failures,
      actual: report.aggregate.step_failures,
    });
  }
  if (policy.max_repair_requests !== undefined && report.aggregate.repair_requests > policy.max_repair_requests) {
    findings.push({
      check_id: "max_repair_requests",
      path: "$.aggregate.repair_requests",
      message: "Repair request events exceed the configured maximum.",
      expected: policy.max_repair_requests,
      actual: report.aggregate.repair_requests,
    });
  }
  if (policy.max_avg_duration_ms !== undefined) {
    for (const [profileId, aggregate] of Object.entries(report.by_profile)) {
      if (aggregate.avg_duration_ms > policy.max_avg_duration_ms) {
        findings.push({
          check_id: "max_avg_duration_ms",
          path: `$.by_profile.${profileId}.avg_duration_ms`,
          message: "Average profile duration exceeds the configured maximum.",
          expected: policy.max_avg_duration_ms,
          actual: aggregate.avg_duration_ms,
        });
      }
    }
  }
  for (const profileId of policy.require_profiles ?? []) {
    if (!report.by_profile[profileId]) {
      findings.push({
        check_id: "require_profiles",
        path: "$.by_profile",
        message: "Required architecture profile is missing from the report.",
        expected: profileId,
        actual: Object.keys(report.by_profile).sort(),
      });
    }
  }
  const scenarioIds = new Set(report.rows.map((row) => row.scenario_id));
  for (const scenarioId of policy.require_scenarios ?? []) {
    if (!scenarioIds.has(scenarioId)) {
      findings.push({
        check_id: "require_scenarios",
        path: "$.rows[*].scenario_id",
        message: "Required scenario is missing from the report.",
        expected: scenarioId,
        actual: [...scenarioIds].sort(),
      });
    }
  }

  return {
    accepted: findings.length === 0,
    suite_id: report.suite_id,
    completion_rate: roundRate(completionRate),
    findings,
  };
}

function roundRate(value: number): number {
  return Math.round(value * 10000) / 10000;
}
