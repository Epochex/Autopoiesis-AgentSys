import type { Skill } from "../skills/types.js";

export interface SkillPerformanceStats {
  attempts: number;
  successes: number;
  wrong_invocations: number;
  bypasses: number;
  unsafe_blocks: number;
  total_token_cost: number;
  total_latency_ms: number;
  last_success_at?: string;
}

export interface SkillProfile {
  skill: Pick<Skill, "name" | "version" | "description" | "permissions">;
  tags: string[];
  stats: SkillPerformanceStats;
  prior?: number;
}

export interface SkillAttentionQuery {
  task_id: string;
  objective: string;
  tags: string[];
  risk: number;
  topK: number;
  maxRisk?: "read_only" | "local_write" | "network" | "side_effect" | "privileged";
}

export interface SkillAttentionDecision {
  selected: SkillProfile[];
  hidden: SkillProfile[];
  scores: Array<{
    skill_name: string;
    score: number;
    reasons: string[];
  }>;
  expected_irrelevant_exposure_reduction: number;
}

export interface SkillOutcomeUpdate {
  skill_name: string;
  success: boolean;
  wrong_invocation?: boolean;
  bypassed?: boolean;
  unsafe_blocked?: boolean;
  token_cost?: number;
  latency_ms?: number;
  happened_at?: string;
}
