import { compileWorkItemToTask, type AgentWorkItem } from "../core/workItem.js";
import type { DefaultOrchiterKernel } from "../kernel/defaultKernel.js";
import { HarnessRunner } from "./runner.js";
import type { HarnessRow } from "./types.js";

export interface ArchitectureProfile {
  profile_id: string;
  label: string;
  tags?: string[];
  buildKernel?: () => DefaultOrchiterKernel | Promise<DefaultOrchiterKernel>;
  buildRuntime?: () => DefaultOrchiterKernel | Promise<DefaultOrchiterKernel>;
}

export interface HarnessScenario {
  scenario_id: string;
  work_item: AgentWorkItem;
  tags?: string[];
}

export interface ScenarioSuite {
  suite_id: string;
  description?: string;
  scenarios: HarnessScenario[];
}

export interface MatrixHarnessRow extends HarnessRow {
  suite_id: string;
  scenario_id: string;
  profile_id: string;
  profile_label: string;
  tags: string[];
}

export interface MatrixHarnessReport {
  started_at: string;
  ended_at: string;
  suite_id: string;
  rows: MatrixHarnessRow[];
  aggregate: {
    profiles: number;
    scenarios: number;
    runs: number;
    completed: number;
    failed: number;
    approval_required: number;
    step_failures: number;
    repair_requests: number;
  };
  by_profile: Record<
    string,
    {
      runs: number;
      completed: number;
      failed: number;
      approval_required: number;
      step_failures: number;
      repair_requests: number;
      avg_duration_ms: number;
    }
  >;
}

export class MatrixHarnessRunner {
  constructor(private readonly profiles: ArchitectureProfile[]) {}

  async runSuite(suite: ScenarioSuite): Promise<MatrixHarnessReport> {
    const startedAt = new Date().toISOString();
    const rows: MatrixHarnessRow[] = [];
    for (const profile of this.profiles) {
      const orchestriterKernel = await buildProfileKernel(profile);
      const report = await new HarnessRunner(orchestriterKernel.kernel).runCases(
        suite.scenarios.map((scenario) => ({
          case_id: scenario.scenario_id,
          task: compileWorkItemToTask(scenario.work_item),
          ...(scenario.tags ? { tags: scenario.tags } : {}),
        })),
      );
      rows.push(...report.rows.map((row) => matrixRow(suite, profile, row)));
    }
    const endedAt = new Date().toISOString();
    return {
      started_at: startedAt,
      ended_at: endedAt,
      suite_id: suite.suite_id,
      rows,
      aggregate: {
        profiles: this.profiles.length,
        scenarios: suite.scenarios.length,
        runs: rows.length,
        completed: rows.filter((row) => row.status === "completed").length,
        failed: rows.filter((row) => row.status === "failed").length,
        approval_required: rows.filter((row) => row.status === "waiting_for_approval").length,
        step_failures: rows.reduce((total, row) => total + row.step_failures, 0),
        repair_requests: rows.reduce((total, row) => total + row.repair_requests, 0),
      },
      by_profile: aggregateByProfile(rows),
    };
  }
}

function matrixRow(suite: ScenarioSuite, profile: ArchitectureProfile, row: HarnessRow): MatrixHarnessRow {
  const scenario = suite.scenarios.find((item) => item.scenario_id === row.case_id);
  return {
    ...row,
    suite_id: suite.suite_id,
    scenario_id: row.case_id,
    profile_id: profile.profile_id,
    profile_label: profile.label,
    tags: [...(profile.tags ?? []), ...(scenario?.tags ?? [])],
  };
}

async function buildProfileKernel(profile: ArchitectureProfile): Promise<DefaultOrchiterKernel> {
  const builder = profile.buildKernel ?? profile.buildRuntime;
  if (!builder) throw new Error(`Architecture profile ${profile.profile_id} does not define a kernel builder`);
  return builder();
}

function aggregateByProfile(rows: MatrixHarnessRow[]): MatrixHarnessReport["by_profile"] {
  const grouped: MatrixHarnessReport["by_profile"] = {};
  for (const row of rows) {
    const existing = grouped[row.profile_id] ?? {
      runs: 0,
      completed: 0,
      failed: 0,
      approval_required: 0,
      step_failures: 0,
      repair_requests: 0,
      avg_duration_ms: 0,
    };
    existing.runs += 1;
    existing.completed += row.status === "completed" ? 1 : 0;
    existing.failed += row.status === "failed" ? 1 : 0;
    existing.approval_required += row.status === "waiting_for_approval" ? 1 : 0;
    existing.step_failures += row.step_failures;
    existing.repair_requests += row.repair_requests;
    existing.avg_duration_ms += row.duration_ms;
    grouped[row.profile_id] = existing;
  }
  for (const aggregate of Object.values(grouped)) {
    aggregate.avg_duration_ms =
      aggregate.runs > 0 ? Math.round((aggregate.avg_duration_ms / aggregate.runs) * 100) / 100 : 0;
  }
  return grouped;
}

export function scenarioFromWorkItem(workItem: AgentWorkItem, tags: string[] = []): HarnessScenario {
  return {
    scenario_id: workItem.work_item_id,
    work_item: workItem,
    tags,
  };
}
