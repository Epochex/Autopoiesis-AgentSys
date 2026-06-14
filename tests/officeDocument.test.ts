import assert from "node:assert/strict";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildOfficeWorkflowTask,
  createDocumentComposeSkill,
} from "../src/index.js";

test("office workflow composes an asynchronous handoff document", async () => {
  const skills = new SkillRegistry();
  skills.register(createDocumentComposeSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });
  const task = buildOfficeWorkflowTask({
    task_id: "office_task_001",
    title: "Draft agent rollout handoff",
    brief: "Prepare an agent rollout handoff for a platform engineering team.",
    audience: "platform engineering",
    sections: ["Summary", "Risks", "Next Actions"],
  });

  const state = await kernel.run(task);
  const output = JSON.stringify(state.plan?.steps.find((step) => step.step_id === "step_execute_primary")?.output);

  assert.equal(state.status, "completed");
  assert.equal(state.task.domain, "office");
  assert.match(output, /Summary/);
  assert.match(output, /Next Actions/);
  assert.match(output, /platform engineering/);
});
