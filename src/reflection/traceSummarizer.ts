import { createHash } from "node:crypto";
import type { EvolutionTraceStep } from "../evolution/types.js";
import type { ReflectionLesson, TraceReflectionInput } from "./types.js";

export function summarizeTraceLessons(input: TraceReflectionInput): ReflectionLesson[] {
  const trace = input.trace;
  const minConfidence = input.minConfidence ?? 0.45;
  const lessons = [
    ...trace.steps.filter((step) => step.kind === "retrieve_memory" || step.kind === "select_context").map((step) => lessonFromStep(trace.trace_id, step, "context")),
    ...trace.steps.filter((step) => step.kind === "skill_patch").map((step) => lessonFromStep(trace.trace_id, step, "skill")),
    ...trace.steps.filter((step) => step.kind === "repair").map((step) => lessonFromStep(trace.trace_id, step, "repair")),
    ...trace.steps.filter((step) => step.kind === "stop").map((step) => lessonFromStep(trace.trace_id, step, "stop")),
  ];
  return lessons.filter((lesson) => lesson.confidence >= minConfidence);
}

function lessonFromStep(traceId: string, step: EvolutionTraceStep, scope: ReflectionLesson["scope"]): ReflectionLesson {
  const rejectedReasons = [
    ...(step.verifier_pass ? [] : ["verifier_failed"]),
    ...(step.unsupported_claims > 0 ? [`unsupported_claims:${step.unsupported_claims}`] : []),
    ...(step.unsafe_actions > 0 ? [`unsafe_actions:${step.unsafe_actions}`] : []),
  ];
  const confidence = clamp(
    0.45 +
      step.branch_coverage_delta +
      (step.verifier_pass ? 0.2 : -0.3) +
      (step.memory_reuse_success ? 0.1 : 0) -
      step.unsupported_claims * 0.2 -
      step.unsafe_actions * 0.5 -
      step.token_cost / 12000,
    0,
    1,
  );
  return {
    lesson_id: `lesson:${hashJson([traceId, step.step_id, scope]).slice(0, 16)}`,
    source_trace_ids: [traceId],
    scope,
    summary: `${scope} lesson from ${step.kind}: ${step.action}`,
    reusable_rule: buildRule(step, scope),
    confidence: round(confidence),
    evidence_refs: [step.step_id],
    rejected_reasons: rejectedReasons,
    metadata: { branch_coverage_delta: step.branch_coverage_delta, token_cost: step.token_cost },
  };
}

function buildRule(step: EvolutionTraceStep, scope: ReflectionLesson["scope"]): string {
  if (scope === "context") return `Prefer context actions like "${step.action}" when they increase branch coverage without unsupported claims.`;
  if (scope === "skill") return `Promote skill changes only after deployed traces show verifier-passing use, not just static skill content.`;
  if (scope === "repair") return `When verifier failures appear, repair by adding missing evidence before expanding tools or specialists.`;
  if (scope === "stop") return `Stop only after required branches are covered and verifier risk is below threshold.`;
  return `Reuse "${step.action}" only when the current trace matches its evidence pattern.`;
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
