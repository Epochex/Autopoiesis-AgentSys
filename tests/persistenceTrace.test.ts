import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  AgentKernel,
  CompositeEventSink,
  ContractReviewer,
  FileCheckpointStore,
  FileRunStore,
  HarnessRunner,
  InMemoryEventSink,
  JsonlEventSink,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildNetOpsInvestigationTask,
  createEchoSkill,
  exportTrace,
} from "../src/index.js";
import type { Skill } from "../src/index.js";

test("file checkpoints and jsonl events persist a completed run", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-persist-"));
  const checkpoints = new FileCheckpointStore({ rootDir });
  const jsonl = new JsonlEventSink({ rootDir });
  const memoryEvents = new InMemoryEventSink();
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    checkpoints,
    events: new CompositeEventSink([jsonl, memoryEvents]),
  });
  const task = buildNetOpsInvestigationTask({
    case_id: "case_persist_001",
    title: "Persist traceable run",
    objective: "Persist the run and trace.",
    evidence_refs: ["ev_1"],
  });

  const state = await kernel.run(task);
  const loaded = await checkpoints.load(state.run_id);
  const eventLines = (await readFile(jsonl.path(), "utf8")).trim().split("\n");
  const trace = exportTrace(state.events);

  assert.equal(loaded?.run_id, state.run_id);
  assert.equal(eventLines.length, state.events.length);
  assert.equal(memoryEvents.events.length, state.events.length);
  assert.equal(trace.spans.length, state.events.length);
  assert.equal(trace.run_id, state.run_id);
});

test("file run store atomically exposes checkpoint and event listing APIs", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-run-store-"));
  const runStore = new FileRunStore({ rootDir });
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    runStore,
  });
  const task = buildNetOpsInvestigationTask({
    case_id: "case_run_store_001",
    title: "Run store task",
    objective: "Persist through durable run store.",
    evidence_refs: ["ev_1"],
  });

  const state = await kernel.run(task);
  const loaded = await runStore.load(state.run_id);
  const events = await runStore.listEvents(state.run_id);
  const runs = await runStore.listRuns();

  assert.equal(loaded?.run_id, state.run_id);
  assert.equal(events.length, state.events.length);
  assert.ok(runs.some((run) => run.run_id === state.run_id));
});

test("harness can run with durable checkpoint store", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-harness-"));
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    checkpoints: new FileCheckpointStore({ rootDir }),
    events: new JsonlEventSink({ rootDir }),
  });
  const runner = new HarnessRunner(kernel);
  const task = buildNetOpsInvestigationTask({
    case_id: "case_persist_002",
    title: "Harness durable run",
    objective: "Run a durable harness case.",
    evidence_refs: ["ev_1"],
  });

  const report = await runner.runCases([{ case_id: task.task_id, task }]);

  assert.equal(report.aggregate.completed, 1);
});

test("approval resume persists through durable run store checkpoints and events", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-approval-resume-"));
  const runStore = new FileRunStore({ rootDir });
  let executions = 0;
  const sideEffectSkill: Skill = {
    name: "persistent_write",
    version: "0.1.0",
    description: "Test-only persistent side-effect skill.",
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object", properties: { written: { type: "boolean" } } },
    permissions: [
      {
        permission: "local.write",
        risk: "local_write",
        description: "Writes to a durable resource.",
        approval_required: true,
      },
    ],
    async invoke() {
      executions += 1;
      return {
        status: "ok",
        output: { written: true },
        observations: [],
      };
    },
  };
  const skills = new SkillRegistry();
  skills.register(sideEffectSkill);
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    runStore,
  });

  const waiting = await kernel.run({
    task_id: "task_persist_approval_resume",
    title: "Persist approval resume",
    objective: "Persist approval, resume, and completion events.",
    input: { skill_refs: ["persistent_write"] },
  });
  const loadedWaiting = await runStore.load(waiting.run_id);
  const resumed = await kernel.approveAndResume({
    runId: waiting.run_id,
    approvedPermissions: ["local.write"],
    approvedBy: "test",
  });
  const loadedCompleted = await runStore.load(waiting.run_id);
  const events = await runStore.listEvents(waiting.run_id);

  assert.equal(waiting.status, "waiting_for_approval");
  assert.equal(loadedWaiting?.status, "waiting_for_approval");
  assert.equal(resumed?.status, "completed");
  assert.equal(loadedCompleted?.status, "completed");
  assert.equal(executions, 1);
  assert.ok(events.some((event) => event.type === "approval_granted"));
  assert.ok(events.some((event) => event.type === "run_resumed"));
  assert.ok(events.some((event) => event.type === "run_completed"));
});

test("approval-gated runs can be cancelled durably before approval", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-cancel-run-"));
  const runStore = new FileRunStore({ rootDir });
  const sideEffectSkill: Skill = {
    name: "cancelled_write",
    version: "0.1.0",
    description: "Test-only cancelled side-effect skill.",
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object", properties: {} },
    permissions: [
      {
        permission: "local.write",
        risk: "local_write",
        description: "Writes to a durable resource.",
        approval_required: true,
      },
    ],
    async invoke() {
      throw new Error("should not execute before approval");
    },
  };
  const skills = new SkillRegistry();
  skills.register(sideEffectSkill);
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    runStore,
  });

  const waiting = await kernel.run({
    task_id: "task_cancel_approval",
    title: "Cancel approval wait",
    objective: "Cancel a run waiting for approval.",
    input: { skill_refs: ["cancelled_write"] },
  });
  const cancelled = await kernel.cancelRun({
    runId: waiting.run_id,
    cancelledBy: "test",
    reason: "approval denied",
  });
  const loaded = await runStore.load(waiting.run_id);
  const events = await runStore.listEvents(waiting.run_id);

  assert.equal(waiting.status, "waiting_for_approval");
  assert.equal(cancelled?.status, "cancelled");
  assert.equal(loaded?.status, "cancelled");
  assert.ok(events.some((event) => event.type === "run_cancelled" && event.payload.from_status === "waiting_for_approval"));
  await assert.rejects(
    () => kernel.approveAndResume({ runId: waiting.run_id, approvedPermissions: ["local.write"] }),
    /expected waiting_for_approval|already terminal|cancelled/,
  );
});
