import assert from "node:assert/strict";
import test from "node:test";
import {
  EnterpriseMemoryGraph,
  buildPolicyCandidate,
  compileContextPacket,
  decideSpecialistTopology,
  redistributeTraceRewards,
  type ContextEvidenceItem,
  type EvolutionTrace,
} from "../src/index.js";

const trace: EvolutionTrace = {
  trace_id: "trace_enterprise_001",
  objective: "Investigate a high-value enterprise workflow failure with bounded context.",
  domain: "enterprise_investigation",
  outcome: "accepted",
  tags: ["context", "verifier", "memory"],
  steps: [
    {
      step_id: "s1",
      kind: "select_context",
      action: "compile selected/excluded/missing context packet",
      branch_coverage_delta: 0.55,
      verifier_pass: true,
      human_accepted: false,
      memory_reuse_success: false,
      unsupported_claims: 0,
      missing_evidence: 1,
      token_cost: 620,
      latency_ms: 12,
      unsafe_actions: 0,
    },
    {
      step_id: "s2",
      kind: "verify",
      action: "reject unsupported claim and request evidence promotion",
      branch_coverage_delta: 0.1,
      verifier_pass: false,
      human_accepted: false,
      memory_reuse_success: true,
      unsupported_claims: 1,
      missing_evidence: 1,
      token_cost: 240,
      latency_ms: 8,
      unsafe_actions: 0,
    },
    {
      step_id: "s3",
      kind: "repair",
      action: "repair claim with promoted evidence and human-gated action",
      branch_coverage_delta: 0.2,
      verifier_pass: true,
      human_accepted: true,
      memory_reuse_success: true,
      unsupported_claims: 0,
      missing_evidence: 0,
      token_cost: 420,
      latency_ms: 18,
      unsafe_actions: 0,
    },
  ],
};

const evidence: ContextEvidenceItem[] = [
  {
    evidence_id: "ev_context_boundary",
    content: "Context boundary marks selected, excluded, and missing evidence surfaces.",
    branch: ["context", "audit"],
    risk: 0.9,
    token_estimate: 120,
    provenance: "trace:001",
  },
  {
    evidence_id: "ev_memory_conflict",
    content: "Historical memory conflicts with the current provider draft.",
    branch: ["memory", "conflict"],
    risk: 0.85,
    token_estimate: 100,
    provenance: "memory:case-17",
  },
  {
    evidence_id: "ev_latency",
    content: "Provider latency is high but the claim is low risk.",
    branch: ["provider", "latency"],
    risk: 0.3,
    token_estimate: 90,
    provenance: "provider:run-3",
  },
];

test("memory graph ingests traces and retrieves utility-ranked memories", () => {
  const graph = new EnterpriseMemoryGraph();
  const caseNode = graph.ingestTrace(trace);
  const results = graph.retrieve({ text: "verifier context memory", tags: ["verifier"], limit: 3 });

  assert.equal(caseNode.kind, "case");
  assert.ok(results.length >= 1);
  assert.ok(results[0]?.score ?? 0);
  assert.match(results.map((item) => item.node.node_id).join(" "), /trace_enterprise_001/);
  assert.ok(graph.snapshot().edges.length >= trace.steps.length);
});

test("context compiler preserves branches and records excluded and missing surfaces", () => {
  const graph = new EnterpriseMemoryGraph();
  graph.ingestTrace(trace);
  const memory = graph.retrieve({ text: "context memory verifier", tags: ["context"], limit: 2 });
  const packet = compileContextPacket({
    objective: trace.objective,
    evidence,
    memory,
    budget: {
      max_items: 2,
      max_tokens: 240,
      required_branches: ["context", "audit", "memory", "conflict", "provider"],
    },
    missing: ["human:approval_note"],
  });

  assert.equal(packet.selected.length, 2);
  assert.equal(packet.excluded.length, 1);
  assert.ok(packet.branch_coverage >= 0.8);
  assert.ok(packet.missing.includes("human:approval_note"));
  assert.ok(packet.audit.excluded_ids.includes("ev_latency"));
});

test("topology gate uses multi-agent coordination only for hard cases", () => {
  const simple = decideSpecialistTopology({
    risk: 0.2,
    branch_coverage: 0.95,
    verifier_rejections: 0,
    provider_disagreement: 0,
    memory_conflict: 0,
    budget_pressure: 0.2,
  });
  const hard = decideSpecialistTopology({
    risk: 0.9,
    branch_coverage: 0.62,
    verifier_rejections: 2,
    provider_disagreement: 0.2,
    memory_conflict: 0.6,
    budget_pressure: 0.4,
  });

  assert.equal(simple.mode, "single_orchestrator");
  assert.equal(hard.mode, "critic_loop");
  assert.ok(hard.specialists.includes("verifier_critic"));
});

test("policy lab turns replay rewards into a release-gated candidate", () => {
  const graph = new EnterpriseMemoryGraph();
  graph.ingestTrace(trace);
  const packet = compileContextPacket({
    objective: trace.objective,
    evidence,
    memory: graph.retrieve({ text: "context memory", limit: 2 }),
    budget: { max_items: 3, max_tokens: 400 },
  });
  const topology = decideSpecialistTopology({
    risk: 0.9,
    branch_coverage: packet.branch_coverage,
    verifier_rejections: 2,
    provider_disagreement: 0,
    memory_conflict: 0.6,
    budget_pressure: 0.3,
  });
  const rewards = redistributeTraceRewards(trace.steps);
  const candidate = buildPolicyCandidate({
    traces: [trace],
    packets: [packet],
    topologies: [topology],
  });

  assert.equal(rewards.length, trace.steps.length);
  assert.equal(candidate.policy_kind, "topology_gate");
  assert.equal(candidate.safety_pass, true);
  assert.deepEqual(candidate.patch.release_gate, ["unit", "replay", "verifier_safety", "cost_regression", "human_approval"]);
});
