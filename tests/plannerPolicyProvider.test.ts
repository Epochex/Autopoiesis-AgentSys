import assert from "node:assert/strict";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  ModelPlanner,
  OpenAICompatibleJsonClient,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  createEchoSkill,
  deriveHealthUrl,
  runJsonProviderSmoke,
} from "../src/index.js";
import type { ChatJsonResult, ChatMessage, JsonModelClient, ProviderHealth, Skill } from "../src/index.js";

class FakeModelClient implements JsonModelClient {
  constructor(private readonly parsed: Record<string, unknown>) {}

  async chatJson(_messages: ChatMessage[]): Promise<ChatJsonResult> {
    return {
      parsed: this.parsed,
      metadata: {
        provider: "fake",
        model: "fake-json-planner",
        status: 200,
        latency_ms: 1,
      },
    };
  }
}

test("model planner parses a bounded DAG and falls back on invalid output", async () => {
  const task = {
    task_id: "task_model_plan",
    title: "Model planned task",
    objective: "Plan with model output.",
    input: { text: "hello", skill_refs: ["echo"] },
  };
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  const planner = new ModelPlanner({
    model: new FakeModelClient({
      strategy: "Use an executor step with the echo skill.",
      steps: [
        {
          step_id: "step_echo",
          agent_role: "executor",
          title: "Echo the task text",
          depends_on: [],
          skill_refs: ["echo"],
          input: { text: "hello" },
        },
      ],
    }),
    skills,
    availableRoles: ["executor"],
  });

  const plan = await planner.createPlan(task);
  const fallbackPlan = await new ModelPlanner({ model: new FakeModelClient({ nope: true }), fallback: new StaticPlanner() }).createPlan(task);

  assert.equal(plan.steps[0]?.step_id, "step_echo");
  assert.equal(plan.steps[0]?.skill_refs[0], "echo");
  assert.equal(plan.metadata?.source, "model");
  assert.equal(fallbackPlan.steps[0]?.step_id, "step_plan_context");
  assert.equal(fallbackPlan.metadata?.source, "fallback");
});

test("model planner rejects unavailable roles and falls back before kernel execution", async () => {
  const planner = new ModelPlanner({
    model: new FakeModelClient({
      strategy: "Use unavailable worker.",
      steps: [
        {
          step_id: "step_bad",
          agent_role: "unregistered_agent",
          title: "Bad role",
          depends_on: [],
          skill_refs: [],
          input: {},
        },
      ],
    }),
    availableRoles: ["executor"],
  });

  const plan = await planner.createPlan({
    task_id: "task_bad_role",
    title: "Bad role task",
    objective: "Should fallback.",
    input: {},
  });

  assert.equal(plan.metadata?.source, "fallback");
  assert.equal(plan.steps[0]?.agent_role, "executor");
});

test("skill policy routes approval-required tools without executing them", async () => {
  const sideEffectSkill: Skill = {
    name: "dangerous_write",
    version: "0.1.0",
    description: "Test-only side-effect skill.",
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object", properties: {} },
    permissions: [
      {
        permission: "local.write",
        risk: "local_write",
        description: "Writes to a local resource.",
        approval_required: true,
      },
    ],
    async invoke() {
      throw new Error("should not execute without approval");
    },
  };
  const skills = new SkillRegistry();
  skills.register(sideEffectSkill);
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });

  const state = await kernel.run({
    task_id: "task_approval_required",
    title: "Approval required task",
    objective: "Attempt to call a side-effect skill.",
    input: { skill_refs: ["dangerous_write"] },
  });

  assert.equal(state.status, "waiting_for_approval");
  assert.equal(state.review?.status, "needs_approval");
});

test("approval-gated runs can resume after explicit permission grant", async () => {
  let executions = 0;
  const sideEffectSkill: Skill = {
    name: "approved_write",
    version: "0.1.0",
    description: "Test-only approved side-effect skill.",
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object", properties: { written: { type: "boolean" } } },
    permissions: [
      {
        permission: "local.write",
        risk: "local_write",
        description: "Writes to a local resource.",
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
  });

  const waiting = await kernel.run({
    task_id: "task_approval_resume",
    title: "Approval resume task",
    objective: "Resume after a local write is approved.",
    input: { skill_refs: ["approved_write"] },
  });
  const resumed = await kernel.approveAndResume({
    runId: waiting.run_id,
    approvedPermissions: ["local.write"],
    approvedBy: "test",
    reason: "unit test approval",
  });

  assert.equal(waiting.status, "waiting_for_approval");
  assert.equal(resumed?.status, "completed");
  assert.equal(executions, 1);
  assert.ok(resumed?.events.some((event) => event.type === "approval_granted"));
  assert.ok(resumed?.events.some((event) => event.type === "run_resumed"));
  assert.ok(resumed?.events.some((event) => event.type === "run_completed"));
  assert.deepEqual(resumed?.task.input.approved_permissions, ["local.write"]);
});

test("failed skill runs enter repair state and resume after input patch", async () => {
  let executions = 0;
  const repairableSkill: Skill = {
    name: "repairable_skill",
    version: "0.1.0",
    description: "Test-only repairable skill.",
    input_schema: { type: "object", properties: { repaired: { type: "boolean" } } },
    output_schema: { type: "object", properties: { repaired: { type: "boolean" } } },
    permissions: [
      {
        permission: "repairable.read",
        risk: "read_only",
        description: "Reads repairable input.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      executions += 1;
      if (invocation.input.repaired !== true) {
        return {
          status: "error",
          output: {},
          observations: [],
          error: "missing repaired flag",
        };
      }
      return {
        status: "ok",
        output: { repaired: true },
        observations: [],
      };
    },
  };
  const skills = new SkillRegistry();
  skills.register(repairableSkill);
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });

  const repairing = await kernel.run({
    task_id: "task_repair_resume",
    title: "Repair resume task",
    objective: "Patch input and resume a failed tool step.",
    input: { skill_refs: ["repairable_skill"] },
  });
  const resumed = await kernel.repairAndResume({
    runId: repairing.run_id,
    inputPatch: { repaired: true },
    repairedBy: "test",
    reason: "supply missing repaired flag",
  });

  assert.equal(repairing.status, "repairing");
  assert.equal(repairing.review?.status, "repair");
  assert.equal(resumed?.status, "completed");
  assert.equal(executions, 2);
  assert.equal(resumed?.task.input.repaired, true);
  assert.ok(repairing.events.some((event) => event.type === "repair_requested"));
  assert.ok(resumed?.events.some((event) => event.type === "repair_applied"));
  assert.ok(resumed?.events.some((event) => event.type === "run_resumed"));
  assert.ok(resumed?.events.some((event) => event.type === "run_completed"));
});

test("provider health URL derives from OpenAI-compatible base URLs", () => {
  assert.equal(deriveHealthUrl("http://127.0.0.1:28000/v1"), "http://127.0.0.1:28000/health");
  assert.equal(deriveHealthUrl("http://127.0.0.1:28000/v1/"), "http://127.0.0.1:28000/health");
  assert.equal(deriveHealthUrl("http://127.0.0.1:18080"), "http://127.0.0.1:18080/health");
});

test("provider smoke runner validates health and sentinel JSON", async () => {
  class FakeHealthyClient implements JsonModelClient {
    async healthCheck(): Promise<ProviderHealth> {
      return {
        ok: true,
        status: 200,
        latency_ms: 1,
        endpoint: "memory://health",
      };
    }

    async chatJson(): Promise<ChatJsonResult> {
      return {
        parsed: { ok: true, component: "selfevo-provider-smoke" },
        metadata: {
          provider: "fake",
          model: "fake",
          status: 200,
          latency_ms: 1,
        },
      };
    }
  }

  const report = await runJsonProviderSmoke(new FakeHealthyClient());

  assert.equal(report.ok, true);
  assert.equal(report.assertions.every((assertion) => assertion.ok), true);
});

test("OpenAI-compatible client accepts explicit health URL config", async () => {
  const client = new OpenAICompatibleJsonClient({
    baseUrl: "http://127.0.0.1:1/v1",
    healthUrl: "http://127.0.0.1:1/custom-health",
    model: "fake",
    provider: "fake",
    timeoutMs: 1,
  });

  const health = await client.healthCheck();

  assert.equal(health.endpoint, "http://127.0.0.1:1/custom-health");
});
