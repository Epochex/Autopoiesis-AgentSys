import assert from "node:assert/strict";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  SubprocessSandboxRunner,
  createCliSkill,
  defaultLocalSandboxPolicy,
} from "../src/index.js";

test("subprocess sandbox enforces command allowlist", async () => {
  const runner = new SubprocessSandboxRunner(defaultLocalSandboxPolicy({ allowedCommands: ["node"] }));

  const denied = await runner.run({ command: "sh", args: ["-c", "echo nope"] });
  const allowed = await runner.run({ command: "node", args: ["-e", "console.log('ok')"] });

  assert.equal(denied.status, "policy_denied");
  assert.equal(allowed.status, "ok");
  assert.match(allowed.stdout, /ok/);
});

test("cli skill requires approval before subprocess execution", async () => {
  const runner = new SubprocessSandboxRunner(defaultLocalSandboxPolicy({ allowedCommands: ["node"] }));
  const skills = new SkillRegistry();
  skills.register(createCliSkill(runner));
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });

  const blocked = await kernel.run({
    task_id: "task_cli_blocked",
    title: "Blocked CLI task",
    objective: "Try a CLI call without approval.",
    input: {
      skill_refs: ["cli.run"],
      command: "node",
      args: ["-e", "console.log('blocked')"],
    },
  });
  const approved = await kernel.run({
    task_id: "task_cli_approved",
    title: "Approved CLI task",
    objective: "Run an approved CLI call.",
    input: {
      skill_refs: ["cli.run"],
      command: "node",
      args: ["-e", "console.log('approved')"],
      approved_permissions: ["local.process.spawn"],
    },
  });

  assert.equal(blocked.status, "waiting_for_approval");
  assert.equal(approved.status, "completed");
  assert.match(JSON.stringify(approved.plan?.steps.find((step) => step.step_id === "step_execute_primary")?.output), /approved/);
});
