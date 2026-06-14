import type { AgentTask } from "../../core/types.js";
import { compileWorkItemToTask, type AgentWorkItem } from "../../core/workItem.js";
import type { DecisionScenario } from "../../simulators/decision.js";

export interface DecisionTaskSeed {
  task_id: string;
  title: string;
  scenario: DecisionScenario;
  candidate_action_id: string;
}

export function buildDecisionSimulationTask(seed: DecisionTaskSeed): AgentTask {
  return compileWorkItemToTask(buildDecisionWorkItem(seed));
}

export function buildDecisionWorkItem(seed: DecisionTaskSeed): AgentWorkItem {
  return {
    work_item_id: seed.task_id,
    title: seed.title,
    objective: seed.scenario.objective,
    domain: "decision",
    payload: {
      scenario_id: seed.scenario.scenario_id,
      action_id: seed.candidate_action_id,
      scenario: seed.scenario,
    },
    input_artifacts: [
      {
        artifact_id: `scenario:${seed.scenario.scenario_id}`,
        kind: "scenario",
        role: "simulator_case",
        metadata: {
          budget: seed.scenario.budget,
          max_risk: seed.scenario.max_risk,
        },
      },
    ],
    available_tools: ["decision.simulate"],
    context_refs: [`scenario:${seed.scenario.scenario_id}`],
    memory_scope: {
      scopes: ["domain", "session"],
      query: seed.scenario.objective,
      tags: ["decision"],
    },
    approval_policy: {
      mode: "human_gate",
      required_permissions: ["external.action.execute"],
      reason: "Simulator evaluation is read-only; real-world action execution requires human approval.",
    },
    constraints: [
      "Evaluate candidate decisions against explicit budget and risk constraints.",
      "Report rejected decisions with simulator reasons rather than unsupported intuition.",
    ],
    success_criteria: [
      "Return simulator-backed reward, cost, risk, and acceptance status.",
      "Expose the decision trace for harness comparison.",
    ],
    eval_contract: {
      metrics: ["constraint_satisfaction", "reward", "risk", "simulator_trace_coverage"],
      success_thresholds: {
        constraint_satisfaction: 1,
      },
      regression_tags: ["decision", "ads_growth_simulation"],
    },
  };
}
