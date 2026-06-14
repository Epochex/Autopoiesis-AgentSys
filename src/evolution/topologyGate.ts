import type { SpecialistTopologyDecision, TopologySignal } from "./types.js";

export function decideSpecialistTopology(signal: TopologySignal): SpecialistTopologyDecision {
  const reasons: string[] = [];
  if (signal.branch_coverage < 0.75) reasons.push("low_branch_coverage");
  if (signal.verifier_rejections >= 2) reasons.push("repeated_verifier_rejection");
  if (signal.provider_disagreement >= 0.5) reasons.push("provider_disagreement");
  if (signal.memory_conflict >= 0.5) reasons.push("memory_conflict");
  if (signal.risk >= 0.8) reasons.push("high_risk");

  if (reasons.length === 0 || signal.budget_pressure >= 0.9) {
    return {
      mode: "single_orchestrator",
      specialists: [],
      reasons: signal.budget_pressure >= 0.9 ? ["budget_pressure_forces_single_orchestrator"] : ["simple_case"],
      max_parallelism: 1,
    };
  }

  if (signal.verifier_rejections >= 2 || signal.memory_conflict >= 0.5) {
    return {
      mode: "critic_loop",
      specialists: ["verifier_critic", "memory_curator", "repair_proposer"],
      reasons,
      max_parallelism: 2,
    };
  }

  if (signal.provider_disagreement >= 0.5 && signal.branch_coverage >= 0.75) {
    return {
      mode: "sparse_consensus",
      specialists: ["provider_referee", "context_auditor"],
      reasons,
      max_parallelism: 2,
    };
  }

  return {
    mode: "star",
    specialists: ["context_compressor", "memory_curator", "verifier_critic"],
    reasons,
    max_parallelism: 3,
  };
}
