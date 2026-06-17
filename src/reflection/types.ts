import type { JsonObject } from "../core/types.js";
import type { EvolutionTrace } from "../evolution/types.js";

export interface ReflectionLesson {
  lesson_id: string;
  source_trace_ids: string[];
  scope: "memory" | "context" | "skill" | "stop" | "repair";
  summary: string;
  reusable_rule: string;
  confidence: number;
  evidence_refs: string[];
  rejected_reasons: string[];
  metadata?: JsonObject;
}

export interface PromotionGateInput {
  lesson: ReflectionLesson;
  replay_cases: number;
  reward_delta: number;
  verifier_pass_rate: number;
  regression_failures: number;
}

export interface PromotionGateDecision {
  accepted: boolean;
  reasons: string[];
  min_replay_cases: number;
}

export interface TraceReflectionInput {
  trace: EvolutionTrace;
  minConfidence?: number;
}
