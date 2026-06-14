import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  AgentKernel,
  ContractReviewer,
  FileMemoryStore,
  InMemoryMemoryStore,
  SkillExecutorAgent,
  SkillRegistry,
  StaticPlanner,
  buildContextPack,
  createMemorySearchSkill,
} from "../src/index.js";

test("memory store searches scoped records and builds bounded context packs", async () => {
  const store = new InMemoryMemoryStore();
  await store.put({
    memory_id: "mem_agent_loop",
    scope: "domain",
    subject: "Agent loop",
    content: "Long running agent loops need checkpoints, trace replay, and repair states.",
    tags: ["agent", "loop"],
    metadata: {},
  });
  await store.put({
    memory_id: "mem_irrelevant",
    scope: "global",
    subject: "Unrelated",
    content: "This memory is about gardening.",
    tags: ["other"],
    metadata: {},
  });

  const results = await store.search({ text: "agent checkpoint repair", scopes: ["domain"], limit: 3 });
  const pack = await buildContextPack(store, { text: "agent checkpoint repair", scopes: ["domain"] }, { maxItems: 2, maxChars: 120 });

  assert.equal(results[0]?.record.memory_id, "mem_agent_loop");
  assert.match(pack.compressed, /checkpoints/);
  assert.equal(pack.memories.length, 1);
  assert.ok(pack.token_estimate > 0);
  assert.equal(pack.budget_utilization.items, 1);
});

test("context packs honor token budgets with deterministic estimation", async () => {
  const store = new InMemoryMemoryStore();
  await store.put({
    memory_id: "mem_long_context",
    scope: "domain",
    subject: "Long context",
    content:
      "Agent memory should be trimmed before model calls so long-running sessions stay inside predictable token budgets.",
    tags: ["agent", "context"],
    metadata: {},
  });

  const pack = await buildContextPack(
    store,
    { text: "agent memory token budget", scopes: ["domain"] },
    { maxItems: 1, maxChars: 500, maxTokens: 12 },
  );

  assert.ok(pack.token_estimate <= 12);
  assert.equal(pack.budget_utilization.tokens, pack.token_estimate);
  assert.equal(pack.budget.maxTokens, 12);
});

test("file memory store reloads persisted memory records", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-memory-"));
  const path = join(rootDir, "memory.jsonl");
  const writer = await FileMemoryStore.open({ path });
  await writer.put({
    memory_id: "mem_persistent",
    scope: "session",
    subject: "Persistent memory",
    content: "Session memory survives process restarts.",
    tags: ["session"],
    metadata: {},
  });
  const reader = await FileMemoryStore.open({ path });

  const found = await reader.search({ text: "session memory restarts", scopes: ["session"] });

  assert.equal(found[0]?.record.memory_id, "mem_persistent");
});

test("memory search skill returns context pack through the agent kernel", async () => {
  const store = new InMemoryMemoryStore();
  await store.put({
    memory_id: "mem_context",
    scope: "session",
    subject: "Context compression",
    content: "Compress context by scope and budget before model calls.",
    tags: ["context"],
    metadata: {},
  });
  const skills = new SkillRegistry();
  skills.register(createMemorySearchSkill(store));
  const kernel = new AgentKernel({
    planner: new StaticPlanner(),
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
  });

  const state = await kernel.run({
    task_id: "task_memory_search",
    title: "Search memory",
    objective: "Retrieve context memory.",
    input: {
      skill_refs: ["memory.search"],
      query: "context budget compression",
      scopes: ["session"],
      maxItems: 2,
      maxChars: 200,
    },
  });

  assert.equal(state.status, "completed");
  assert.match(JSON.stringify(state.plan?.steps.find((step) => step.step_id === "step_execute_primary")?.output), /context_id/);
});
