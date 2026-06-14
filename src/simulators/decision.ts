import type { JsonObject } from "../core/types.js";

export interface DecisionAction {
  action_id: string;
  label: string;
  expected_reward: number;
  cost: number;
  risk: number;
  metadata?: JsonObject;
}

export interface DecisionScenario {
  scenario_id: string;
  objective: string;
  budget: number;
  max_risk: number;
  actions: DecisionAction[];
}

export interface DecisionSimulationResult {
  scenario_id: string;
  action_id: string;
  accepted: boolean;
  reward: number;
  cost: number;
  risk: number;
  reasons: string[];
}

export class DecisionSimulator {
  constructor(private readonly scenarios: DecisionScenario[]) {}

  simulate(scenarioId: string, actionId: string): DecisionSimulationResult {
    const scenario = this.scenarios.find((item) => item.scenario_id === scenarioId);
    if (!scenario) return rejected(scenarioId, actionId, `Unknown scenario: ${scenarioId}`);
    const action = scenario.actions.find((item) => item.action_id === actionId);
    if (!action) return rejected(scenarioId, actionId, `Unknown action: ${actionId}`);
    const reasons = [];
    if (action.cost > scenario.budget) reasons.push(`cost ${action.cost} exceeds budget ${scenario.budget}`);
    if (action.risk > scenario.max_risk) reasons.push(`risk ${action.risk} exceeds max risk ${scenario.max_risk}`);
    return {
      scenario_id: scenarioId,
      action_id: actionId,
      accepted: reasons.length === 0,
      reward: action.expected_reward,
      cost: action.cost,
      risk: action.risk,
      reasons,
    };
  }

  getScenario(scenarioId: string): DecisionScenario | undefined {
    const scenario = this.scenarios.find((item) => item.scenario_id === scenarioId);
    return scenario ? structuredClone(scenario) : undefined;
  }
}

function rejected(scenarioId: string, actionId: string, reason: string): DecisionSimulationResult {
  return {
    scenario_id: scenarioId,
    action_id: actionId,
    accepted: false,
    reward: 0,
    cost: 0,
    risk: 1,
    reasons: [reason],
  };
}
