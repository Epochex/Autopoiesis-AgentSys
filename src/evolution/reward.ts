import type { EvolutionStepKind, EvolutionTraceStep, RewardWeights, StepReward } from "./types.js";

export const DEFAULT_REWARD_WEIGHTS: RewardWeights = {
  branch_coverage_gain: 3,
  verifier_pass: 1.5,
  human_acceptance: 2,
  memory_reuse_success: 1,
  unsupported_claim_penalty: 2.5,
  missing_evidence_penalty: 1.2,
  token_cost_penalty: 0.0005,
  latency_cost_penalty: 0.0002,
  unsafe_action_penalty: 5,
};

export function scoreStepReward(step: EvolutionTraceStep, weights: RewardWeights = DEFAULT_REWARD_WEIGHTS): StepReward {
  const components: Record<string, number> = {
    branch_coverage_gain: step.branch_coverage_delta * weights.branch_coverage_gain,
    verifier_pass: step.verifier_pass ? weights.verifier_pass : 0,
    human_acceptance: step.human_accepted ? weights.human_acceptance : 0,
    memory_reuse_success: step.memory_reuse_success ? weights.memory_reuse_success : 0,
    unsupported_claim_penalty: -step.unsupported_claims * weights.unsupported_claim_penalty,
    missing_evidence_penalty: -step.missing_evidence * weights.missing_evidence_penalty,
    token_cost_penalty: -step.token_cost * weights.token_cost_penalty,
    latency_cost_penalty: -step.latency_ms * weights.latency_cost_penalty,
    unsafe_action_penalty: -step.unsafe_actions * weights.unsafe_action_penalty,
  };
  return {
    step_id: step.step_id,
    reward: round(Object.values(components).reduce((sum, value) => sum + value, 0)),
    components,
  };
}

export function redistributeTraceRewards(steps: EvolutionTraceStep[], weights: RewardWeights = DEFAULT_REWARD_WEIGHTS): StepReward[] {
  const raw = steps.map((step) => scoreStepReward(step, weights));
  const verifierFailures = steps.filter((step) => !step.verifier_pass).length;
  if (verifierFailures === 0) return raw;

  return raw.map((reward, index) => {
    const step = steps[index];
    const repairCredit = step && isRepairLike(step.kind) ? verifierFailures * 0.4 : 0;
    return {
      ...reward,
      reward: round(reward.reward + repairCredit),
      components: {
        ...reward.components,
        repair_credit: repairCredit,
      },
    };
  });
}

function isRepairLike(kind: EvolutionStepKind): boolean {
  return kind === "repair" || kind === "retrieve_memory" || kind === "select_context";
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
