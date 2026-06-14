import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtemp } from "node:fs/promises";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildCodingInvestigationTask,
  createWorkspaceSearchSkill,
} from "../src/index.js";

test("coding adapter builds a repository inspection task", async () => {
  const repo = await mkdtemp(join(tmpdir(), "selfevo-coding-"));
  await mkdir(join(repo, "src"));
  await writeFile(join(repo, "src", "agent.ts"), "export function planAgentLoop() { return 'checkpoint repair trace'; }\n", "utf8");
  const skills = new SkillRegistry();
  skills.register(createWorkspaceSearchSkill({ rootDir: repo }));
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });
  const task = buildCodingInvestigationTask({
    task_id: "coding_issue_001",
    title: "Investigate agent loop issue",
    issue: "Find code related to checkpoint repair trace in the agent loop.",
    repository_path: repo,
  });

  const state = await kernel.run(task);

  assert.equal(state.status, "completed");
  assert.equal(state.task.domain, "coding");
  assert.match(JSON.stringify(state.plan?.steps.find((step) => step.step_id === "step_execute_primary")?.output), /agent.ts/);
});

test("workspace search denies paths outside the configured root", async () => {
  const repo = await mkdtemp(join(tmpdir(), "selfevo-coding-root-"));
  const outside = await mkdtemp(join(tmpdir(), "selfevo-coding-outside-"));
  const skill = createWorkspaceSearchSkill({ rootDir: repo });

  const result = await skill.invoke({
    invocation_id: "inv_workspace_outside",
    skill_name: "workspace.search",
    input: {
      query: "secret",
      repository_path: outside,
    },
    context: {
      run_id: "run",
      task_id: "task",
      allowed_resource_refs: [],
      memory_refs: [],
      metadata: {},
    },
  });

  assert.equal(result.status, "error");
  assert.match(result.error ?? "", /outside workspace root/);
});
