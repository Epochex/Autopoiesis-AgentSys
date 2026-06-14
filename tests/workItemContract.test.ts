import assert from "node:assert/strict";
import test from "node:test";
import {
  buildCodingWorkItem,
  buildDecisionWorkItem,
  buildNetOpsWorkItem,
  buildOfficeWorkItem,
  compileWorkItemToTask,
} from "../src/index.js";

test("agent work items compile into a reusable task input contract", () => {
  const workItem = buildCodingWorkItem({
    task_id: "coding_contract",
    title: "Inspect issue",
    issue: "Find workspace search code",
    repository_path: "/tmp/repo",
    target_paths: ["src"],
  });

  const task = compileWorkItemToTask(workItem);

  assert.equal(task.task_id, "coding_contract");
  assert.equal(task.domain, "coding");
  assert.equal(task.input.work_item, workItem);
  assert.deepEqual(task.input.skill_refs, ["workspace.search"]);
  assert.ok(Array.isArray(task.input.input_artifacts));
  assert.equal((task.input.approval_policy as Record<string, unknown>).mode, "human_gate");
  assert.ok(task.input.eval_contract);
});

test("domain work items expose migration-oriented metadata instead of domain-only inputs", () => {
  const office = buildOfficeWorkItem({
    task_id: "office_contract",
    title: "Draft handoff",
    brief: "Prepare a launch note.",
    sections: ["Summary"],
  });
  const decision = buildDecisionWorkItem({
    task_id: "decision_contract",
    title: "Evaluate action",
    candidate_action_id: "safe_action",
    scenario: {
      scenario_id: "growth_case",
      objective: "Evaluate growth action.",
      budget: 10,
      max_risk: 0.2,
      actions: [{ action_id: "safe_action", label: "Safe action", expected_reward: 1, cost: 1, risk: 0.1 }],
    },
  });
  const netops = buildNetOpsWorkItem({
    case_id: "netops_contract",
    title: "Investigate incident",
    objective: "Inspect bounded evidence.",
    evidence_refs: ["ev_1"],
  });

  assert.deepEqual(office.available_tools, ["document.compose"]);
  assert.deepEqual(decision.available_tools, ["decision.simulate"]);
  assert.deepEqual(netops.available_tools, ["echo"]);
  assert.equal(decision.input_artifacts[0]?.kind, "scenario");
  assert.equal(netops.input_artifacts[0]?.kind, "evidence");
  assert.equal(office.eval_contract?.regression_tags?.includes("digital_employee"), true);
});
