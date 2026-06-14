import assert from "node:assert/strict";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  DecisionSimulator,
  HarnessRunner,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildDecisionSimulationTask,
  createDecisionSimulateSkill,
} from "../src/index.js";

const scenario = {
  scenario_id: "ads_budget_shift",
  objective: "Choose an ads optimization action under risk and budget constraints.",
  budget: 100,
  max_risk: 0.4,
  actions: [
    {
      action_id: "increase_budget_10",
      label: "Increase high-performing segment budget by 10 percent",
      expected_reward: 18,
      cost: 40,
      risk: 0.2,
    },
    {
      action_id: "double_budget",
      label: "Double budget immediately",
      expected_reward: 35,
      cost: 160,
      risk: 0.7,
    },
  ],
};

test("decision simulator accepts bounded actions and rejects unsafe actions", () => {
  const simulator = new DecisionSimulator([scenario]);

  const accepted = simulator.simulate("ads_budget_shift", "increase_budget_10");
  const rejected = simulator.simulate("ads_budget_shift", "double_budget");

  assert.equal(accepted.accepted, true);
  assert.equal(rejected.accepted, false);
  assert.match(rejected.reasons.join(" "), /exceeds budget/);
});

test("decision simulation task runs through kernel and harness metrics", async () => {
  const simulator = new DecisionSimulator([scenario]);
  const skills = new SkillRegistry();
  skills.register(createDecisionSimulateSkill(simulator));
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });
  const task = buildDecisionSimulationTask({
    task_id: "decision_task_001",
    title: "Evaluate ads action",
    scenario,
    candidate_action_id: "increase_budget_10",
  });

  const report = await new HarnessRunner(kernel).runCases([{ case_id: task.task_id, task }]);

  assert.equal(report.aggregate.completed, 1);
  assert.equal(report.aggregate.skill_invocations, 1);
  assert.equal(report.rows[0]?.step_count, 2);
});
