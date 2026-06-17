import assert from "node:assert/strict";
import test from "node:test";
import {
  MemoryConsolidator,
  SkillAttentionController,
  buildGrpoGroups,
  evaluateLessonPromotion,
  retrieveMemoryNotes,
  summarizeTraceLessons,
  type EvolutionTrace,
  type SkillProfile,
} from "../src/index.js";

const cleanTrace: EvolutionTrace = {
  trace_id: "trace_memory_clean",
  objective: "Resolve stale context and select the right enterprise memory before calling tools.",
  domain: "agent_memory",
  outcome: "accepted",
  tags: ["memory", "context", "skill_attention"],
  steps: [
    {
      step_id: "m1",
      kind: "retrieve_memory",
      action: "retrieve procedural memory for context conflict resolution",
      branch_coverage_delta: 0.55,
      verifier_pass: true,
      human_accepted: false,
      memory_reuse_success: true,
      unsupported_claims: 0,
      missing_evidence: 0,
      token_cost: 420,
      latency_ms: 24,
      unsafe_actions: 0,
    },
    {
      step_id: "c1",
      kind: "select_context",
      action: "compile minimal evidence packet and exclude stale memory",
      branch_coverage_delta: 0.62,
      verifier_pass: true,
      human_accepted: false,
      memory_reuse_success: true,
      unsupported_claims: 0,
      missing_evidence: 0,
      token_cost: 510,
      latency_ms: 30,
      unsafe_actions: 0,
    },
  ],
};

const contaminatedTrace: EvolutionTrace = {
  trace_id: "trace_memory_contaminated",
  objective: "Resolve stale context and select the right enterprise memory before calling tools.",
  domain: "agent_memory",
  outcome: "rejected",
  tags: ["memory", "context", "stale"],
  steps: [
    {
      step_id: "bad1",
      kind: "retrieve_memory",
      action: "reuse stale dependency note without verifier support",
      branch_coverage_delta: 0.2,
      verifier_pass: false,
      human_accepted: false,
      memory_reuse_success: false,
      unsupported_claims: 2,
      missing_evidence: 1,
      token_cost: 360,
      latency_ms: 14,
      unsafe_actions: 0,
    },
  ],
};

test("memory OS consolidates traces and retrieves clean procedural memory ahead of contaminated notes", () => {
  const consolidator = new MemoryConsolidator();
  const clean = consolidator.consolidateTrace(cleanTrace, { now: new Date("2026-06-14T00:00:00.000Z") });
  const contaminated = consolidator.consolidateTrace(contaminatedTrace, { now: new Date("2026-06-14T00:01:00.000Z") });

  const hits = retrieveMemoryNotes(consolidator.list(), {
    text: "context conflict stale memory retrieve procedural",
    tags: ["memory", "context"],
    tiers: ["procedural", "episodic"],
    limit: 5,
    minConfidence: 0.5,
  });

  assert.ok(clean.notes.length >= 2);
  assert.ok(contaminated.quarantined.length >= 1);
  assert.ok(hits.length >= 1);
  assert.equal(hits.some((hit) => hit.note.source_trace_ids.includes("trace_memory_contaminated")), false);
  assert.equal(hits[0]?.note.tier, "procedural");
});

test("skill attention controller reduces irrelevant skill exposure while keeping required skills visible", () => {
  const controller = new SkillAttentionController(skillProfiles());
  const allSkills = skillProfiles();
  const decision = controller.decide({
    task_id: "task_memory_context",
    objective: "retrieve memory and compile context packet for a stale enterprise trace",
    tags: ["memory", "context"],
    risk: 0.4,
    topK: 2,
    maxRisk: "network",
  });

  const naiveIrrelevantExposure = allSkills.filter((profile) => !profile.tags.some((tag) => ["memory", "context"].includes(tag))).length / allSkills.length;
  const selectedNames = decision.selected.map((profile) => profile.skill.name);

  assert.deepEqual(new Set(selectedNames), new Set(["memory.search", "context.compile"]));
  assert.ok(decision.expected_irrelevant_exposure_reduction > 0.45);
  assert.ok(decision.expected_irrelevant_exposure_reduction >= naiveIrrelevantExposure - 0.01);
});

test("skill attention demotes wrong tool invocations after feedback", () => {
  const controller = new SkillAttentionController(skillProfiles());
  controller.update({
    skill_name: "memory.search",
    success: false,
    wrong_invocation: true,
    token_cost: 800,
    latency_ms: 40,
  });
  controller.update({
    skill_name: "context.compile",
    success: true,
    token_cost: 300,
    latency_ms: 20,
    happened_at: "2026-06-14T00:02:00.000Z",
  });
  const decision = controller.decide({
    task_id: "task_context_only",
    objective: "compile selected evidence packet without memory retrieval",
    tags: ["context"],
    risk: 0.3,
    topK: 1,
  });

  assert.equal(decision.selected[0]?.skill.name, "context.compile");
  assert.ok((decision.scores.find((score) => score.skill_name === "context.compile")?.score ?? 0) > (decision.scores.find((score) => score.skill_name === "memory.search")?.score ?? 0));
});

test("reflection promotion gate accepts only replay-backed lessons", () => {
  const lessons = summarizeTraceLessons({ trace: cleanTrace });
  const accepted = evaluateLessonPromotion({
    lesson: lessons[0]!,
    replay_cases: 3,
    reward_delta: 0.18,
    verifier_pass_rate: 1,
    regression_failures: 0,
  });
  const rejected = evaluateLessonPromotion({
    lesson: {
      ...lessons[0]!,
      rejected_reasons: ["unsupported_claims:1"],
    },
    replay_cases: 3,
    reward_delta: 0.2,
    verifier_pass_rate: 1,
    regression_failures: 0,
  });

  assert.ok(lessons.length >= 2);
  assert.equal(accepted.accepted, true);
  assert.equal(rejected.accepted, false);
  assert.ok(rejected.reasons.some((reason) => reason.startsWith("lesson_rejected")));
});

test("GRPO dataset export creates group-relative advantages for policy training", () => {
  const groups = buildGrpoGroups([cleanTrace, contaminatedTrace]);
  const group = groups[0]!;
  const cleanRollout = group.rollouts.find((rollout) => rollout.rollout_id === cleanTrace.trace_id);
  const badRollout = group.rollouts.find((rollout) => rollout.rollout_id === contaminatedTrace.trace_id);

  assert.equal(groups.length, 1);
  assert.equal(group.rollouts.length, 2);
  assert.ok((cleanRollout?.advantage ?? 0) > 0);
  assert.ok((badRollout?.advantage ?? 0) < 0);
  assert.ok((cleanRollout?.reward ?? 0) > (badRollout?.reward ?? 0));
});

function skillProfiles(): SkillProfile[] {
  return [
    profile("memory.search", "Search scoped memories and return compact procedural or episodic notes.", ["memory", "retrieval"], "read_only", {
      attempts: 10,
      successes: 8,
      wrong_invocations: 1,
      bypasses: 0,
      unsafe_blocks: 0,
      total_token_cost: 2400,
      total_latency_ms: 300,
    }),
    profile("context.compile", "Compile selected evidence into a bounded context packet.", ["context", "evidence"], "read_only", {
      attempts: 8,
      successes: 7,
      wrong_invocations: 0,
      bypasses: 0,
      unsafe_blocks: 0,
      total_token_cost: 1800,
      total_latency_ms: 220,
    }),
    profile("cli.run", "Run an allowlisted local command.", ["cli", "tool"], "local_write", {
      attempts: 4,
      successes: 2,
      wrong_invocations: 1,
      bypasses: 0,
      unsafe_blocks: 1,
      total_token_cost: 1200,
      total_latency_ms: 500,
    }),
    profile("workspace.search", "Search source files in the workspace.", ["coding", "workspace"], "read_only", {
      attempts: 5,
      successes: 4,
      wrong_invocations: 0,
      bypasses: 0,
      unsafe_blocks: 0,
      total_token_cost: 1300,
      total_latency_ms: 260,
    }),
    profile("document.compose", "Draft office handoff documents.", ["office", "document"], "read_only", {
      attempts: 5,
      successes: 4,
      wrong_invocations: 0,
      bypasses: 0,
      unsafe_blocks: 0,
      total_token_cost: 2500,
      total_latency_ms: 180,
    }),
  ];
}

function profile(
  name: string,
  description: string,
  tags: string[],
  risk: "read_only" | "local_write" | "network" | "side_effect" | "privileged",
  stats: SkillProfile["stats"],
): SkillProfile {
  return {
    skill: {
      name,
      version: "0.1.0",
      description,
      permissions: [
        {
          permission: `${name}:use`,
          risk,
          description,
          approval_required: risk !== "read_only",
        },
      ],
    },
    tags,
    stats,
  };
}
