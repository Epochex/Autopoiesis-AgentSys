import type { PromotionGateDecision, PromotionGateInput } from "./types.js";

export function evaluateLessonPromotion(input: PromotionGateInput): PromotionGateDecision {
  const minReplayCases = input.lesson.confidence >= 0.8 ? 2 : 3;
  const reasons = [
    ...(input.lesson.rejected_reasons.length > 0 ? [`lesson_rejected:${input.lesson.rejected_reasons.join(",")}`] : []),
    ...(input.replay_cases < minReplayCases ? [`insufficient_replay_cases:${input.replay_cases}<${minReplayCases}`] : []),
    ...(input.reward_delta <= 0 ? [`non_positive_reward_delta:${input.reward_delta}`] : []),
    ...(input.verifier_pass_rate < 0.9 ? [`low_verifier_pass_rate:${input.verifier_pass_rate}`] : []),
    ...(input.regression_failures > 0 ? [`regression_failures:${input.regression_failures}`] : []),
  ];
  return {
    accepted: reasons.length === 0,
    reasons: reasons.length > 0 ? reasons : ["accepted: replay improved reward without verifier or regression failures"],
    min_replay_cases: minReplayCases,
  };
}
