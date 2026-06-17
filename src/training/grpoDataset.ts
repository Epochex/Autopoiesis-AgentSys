import { redistributeTraceRewards } from "../evolution/reward.js";
import type { EvolutionTrace, StepReward } from "../evolution/types.js";

export interface GrpoRollout {
  rollout_id: string;
  task_id: string;
  trace: EvolutionTrace;
  action_summary: {
    memory_queries: number;
    context_selections: number;
    skill_patches: number;
    unsafe_actions: number;
  };
  reward: number;
  step_rewards: StepReward[];
  advantage?: number;
}

export interface GrpoGroup {
  group_id: string;
  objective: string;
  rollouts: GrpoRollout[];
  reward_mean: number;
}

export function buildGrpoGroups(traces: EvolutionTrace[]): GrpoGroup[] {
  const groups = new Map<string, EvolutionTrace[]>();
  for (const trace of traces) {
    const key = `${trace.domain}:${trace.objective}`;
    groups.set(key, [...(groups.get(key) ?? []), trace]);
  }
  return [...groups.entries()].map(([key, grouped]) => {
    const rollouts = grouped.map((trace) => rolloutFromTrace(trace));
    const rewardMean = mean(rollouts.map((rollout) => rollout.reward));
    return {
      group_id: `grpo:${key}`,
      objective: grouped[0]?.objective ?? key,
      rollouts: rollouts.map((rollout) => ({ ...rollout, advantage: round(rollout.reward - rewardMean) })),
      reward_mean: round(rewardMean),
    };
  });
}

function rolloutFromTrace(trace: EvolutionTrace): GrpoRollout {
  const stepRewards = redistributeTraceRewards(trace.steps);
  const reward = stepRewards.reduce((sum, item) => sum + item.reward, 0);
  return {
    rollout_id: trace.trace_id,
    task_id: `${trace.domain}:${trace.objective}`,
    trace,
    action_summary: {
      memory_queries: trace.steps.filter((step) => step.kind === "retrieve_memory").length,
      context_selections: trace.steps.filter((step) => step.kind === "select_context").length,
      skill_patches: trace.steps.filter((step) => step.kind === "skill_patch").length,
      unsafe_actions: trace.steps.reduce((sum, step) => sum + step.unsafe_actions, 0),
    },
    reward: round(reward),
    step_rewards: stepRewards,
  };
}

function mean(values: number[]): number {
  return values.length === 0 ? 0 : values.reduce((sum, value) => sum + value, 0) / values.length;
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
