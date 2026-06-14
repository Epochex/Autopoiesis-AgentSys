import { createHash } from "node:crypto";
import type {
  CompiledContextPacket,
  EvolutionTrace,
  PolicyCandidate,
  SpecialistTopologyDecision,
} from "./types.js";
import { redistributeTraceRewards } from "./reward.js";

export function buildPolicyCandidate(input: {
  traces: EvolutionTrace[];
  packets: CompiledContextPacket[];
  topologies: SpecialistTopologyDecision[];
  policyKind?: PolicyCandidate["policy_kind"];
}): PolicyCandidate {
  const rewards = input.traces.flatMap((trace) => redistributeTraceRewards(trace.steps));
  const rewardMean = rewards.length === 0 ? 0 : rewards.reduce((sum, item) => sum + item.reward, 0) / rewards.length;
  const unsafe = input.traces.some((trace) => trace.steps.some((step) => step.unsafe_actions > 0));
  const lowCoverage = input.packets.some((packet) => packet.branch_coverage < 0.5);
  const modes = [...new Set(input.topologies.map((topology) => topology.mode))].sort();
  const candidateId = `policy_${hashJson({
    traces: input.traces.map((trace) => trace.trace_id),
    packets: input.packets.map((packet) => packet.packet_id),
    modes,
    rewardMean,
  }).slice(0, 16)}`;

  return {
    candidate_id: candidateId,
    policy_kind: input.policyKind ?? inferPolicyKind(modes),
    summary: summarizeCandidate(rewardMean, unsafe, lowCoverage, modes),
    reward_mean: round(rewardMean),
    safety_pass: !unsafe && !lowCoverage,
    replay_cases: input.traces.length,
    patch: {
      context_budget_hint: input.packets.length > 0 ? averageTokens(input.packets) : 0,
      topology_modes_seen: modes,
      release_gate: ["unit", "replay", "verifier_safety", "cost_regression", "human_approval"],
    },
    evidence: {
      trace_ids: input.traces.map((trace) => trace.trace_id),
      packet_ids: input.packets.map((packet) => packet.packet_id),
      topology_modes: modes,
    },
  };
}

function inferPolicyKind(modes: string[]): PolicyCandidate["policy_kind"] {
  if (modes.some((mode) => mode !== "single_orchestrator")) return "topology_gate";
  return "context_budget";
}

function summarizeCandidate(rewardMean: number, unsafe: boolean, lowCoverage: boolean, modes: string[]): string {
  const safety = unsafe ? "blocked by unsafe action" : lowCoverage ? "blocked by low coverage" : "release-gate eligible";
  return `mean_reward=${round(rewardMean)}; modes=${modes.join(",") || "none"}; ${safety}`;
}

function averageTokens(packets: CompiledContextPacket[]): number {
  return Math.round(packets.reduce((sum, packet) => sum + packet.token_estimate, 0) / packets.length);
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
