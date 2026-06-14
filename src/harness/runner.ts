import { AgentKernel } from "../core/kernel.js";
import type { HarnessCase, HarnessReport, HarnessRow } from "./types.js";

export class HarnessRunner {
  constructor(private readonly kernel: AgentKernel) {}

  async runCases(cases: HarnessCase[]): Promise<HarnessReport> {
    const startedAt = new Date().toISOString();
    const rows: HarnessRow[] = [];
    for (const item of cases) {
      const started = performance.now();
      const state = await this.kernel.run(item.task);
      rows.push({
        case_id: item.case_id,
        run_id: state.run_id,
        status: state.status,
        event_count: state.events.length,
        ...(state.plan?.metadata?.source ? { plan_source: state.plan.metadata.source } : {}),
        step_count: state.plan?.steps.length ?? 0,
        step_failures: state.events.filter((event) => event.type === "step_failed").length,
        repair_requests: state.events.filter((event) => event.type === "repair_requested").length,
        skill_invocations: state.events.filter((event) => event.type === "skill_invoked").length,
        skill_failures: state.events.filter((event) => event.type === "skill_failed").length,
        approval_events: state.events.filter((event) => event.type === "approval_required").length,
        duration_ms: Math.round((performance.now() - started) * 100) / 100,
        ...(state.review ? { review_status: state.review.status } : {}),
      });
    }
    const endedAt = new Date().toISOString();
    return {
      started_at: startedAt,
      ended_at: endedAt,
      rows,
      aggregate: {
        cases: rows.length,
        completed: rows.filter((row) => row.status === "completed").length,
        failed: rows.filter((row) => row.status === "failed").length,
        repair_requested: rows.filter((row) => row.status === "repairing").length,
        approval_required: rows.filter((row) => row.status === "waiting_for_approval").length,
        step_failures: rows.reduce((total, row) => total + row.step_failures, 0),
        repair_requests: rows.reduce((total, row) => total + row.repair_requests, 0),
        skill_invocations: rows.reduce((total, row) => total + row.skill_invocations, 0),
        skill_failures: rows.reduce((total, row) => total + row.skill_failures, 0),
        approval_events: rows.reduce((total, row) => total + row.approval_events, 0),
      },
    };
  }
}
