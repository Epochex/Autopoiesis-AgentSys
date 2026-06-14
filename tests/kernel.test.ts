import assert from "node:assert/strict";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  HarnessRunner,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildNetOpsInvestigationTask,
  createEchoSkill,
  type AgentModule,
  type AgentRunState,
  type AgentStep,
  type JsonObject,
} from "../src/index.js";

test("kernel runs a planned skill-backed task with review and trace events", async () => {
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });
  const task = buildNetOpsInvestigationTask({
    case_id: "case_netops_001",
    title: "Investigate bounded incident evidence",
    objective: "Inspect the incident window and produce a human-gated diagnosis.",
    evidence_refs: ["ev_1", "ev_2"],
  });

  const state = await kernel.run(task);

  assert.equal(state.status, "completed");
  assert.equal(state.review?.status, "accepted");
  assert.ok(state.events.some((event) => event.type === "plan_created"));
  assert.ok(state.events.some((event) => event.type === "skill_invoked"));
  assert.ok(state.events.some((event) => event.type === "skill_completed"));
  assert.ok(state.events.some((event) => event.type === "review_started"));
  assert.ok(state.events.some((event) => event.type === "step_completed" && event.step_id === "step_execute_primary"));
  assert.equal(state.plan?.steps.find((step) => step.step_id === "step_execute_primary")?.status, "succeeded");
  assert.equal(state.plan?.metadata?.source, "static");
});

test("harness aggregates multiple kernel runs", async () => {
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });
  const runner = new HarnessRunner(kernel);
  const task = buildNetOpsInvestigationTask({
    case_id: "case_netops_002",
    title: "Replay incident investigation",
    objective: "Replay a bounded incident task.",
    evidence_refs: ["ev_1"],
  });

  const report = await runner.runCases([{ case_id: "case_netops_002", task }]);

  assert.equal(report.aggregate.cases, 1);
  assert.equal(report.aggregate.completed, 1);
  assert.equal(report.aggregate.skill_invocations, 1);
  assert.equal(report.rows[0]?.status, "completed");
  assert.equal(report.rows[0]?.plan_source, "static");
});

test("kernel retries transient step failures within a bounded policy", async () => {
  const agent = new FlakyExecutorAgent();
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [agent],
    reviewer: new ContractReviewer(),
    retryPolicy: {
      maxAttempts: 2,
      retryableErrorPatterns: ["transient"],
    },
  });
  const task = buildNetOpsInvestigationTask({
    case_id: "case_retry_001",
    title: "Retry transient failure",
    objective: "Recover a transient executor failure.",
    evidence_refs: ["ev_1"],
  });

  const state = await kernel.run(task);

  assert.equal(state.status, "completed");
  assert.equal(agent.primaryAttempts, 2);
  assert.ok(
    state.events.some(
      (event) =>
        event.type === "step_failed" &&
        event.step_id === "step_execute_primary" &&
        event.payload.retryable === true &&
        event.payload.attempt === 1,
    ),
  );
  assert.ok(
    state.events.some(
      (event) =>
        event.type === "step_completed" &&
        event.step_id === "step_execute_primary" &&
        event.payload.attempt === 2,
    ),
  );
});

class FlakyExecutorAgent implements AgentModule {
  readonly role = "executor";
  primaryAttempts = 0;

  async runStep(step: AgentStep, _state: AgentRunState): Promise<JsonObject> {
    if (step.step_id === "step_execute_primary") {
      this.primaryAttempts += 1;
      if (this.primaryAttempts === 1) throw new Error("transient tool failure");
    }
    return { step_id: step.step_id, status: "ok" };
  }
}
