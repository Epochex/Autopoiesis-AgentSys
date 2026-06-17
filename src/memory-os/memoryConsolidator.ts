import { createHash } from "node:crypto";
import type { EvolutionTrace, EvolutionTraceStep } from "../evolution/types.js";
import type { MemoryContaminationReport, MemoryNote, TraceMemoryConsolidation } from "./types.js";

export interface MemoryConsolidatorOptions {
  now?: Date;
  minProceduralReward?: number;
}

export class MemoryConsolidator {
  private readonly notes = new Map<string, MemoryNote>();

  consolidateTrace(trace: EvolutionTrace, options: MemoryConsolidatorOptions = {}): TraceMemoryConsolidation {
    const createdAt = (options.now ?? new Date()).toISOString();
    const notes = [
      buildEpisodicNote(trace, createdAt),
      ...buildProceduralNotes(trace, createdAt, options.minProceduralReward ?? 0.4),
      ...buildSemanticNotes(trace, createdAt),
    ];
    const accepted: MemoryNote[] = [];
    const quarantined: MemoryNote[] = [];
    for (const note of notes) {
      const merged = this.upsert(note);
      if (merged.contamination.contaminated) quarantined.push(merged);
      else accepted.push(merged);
    }
    return { trace, notes: accepted, quarantined };
  }

  upsert(note: MemoryNote): MemoryNote {
    const existing = this.notes.get(note.note_id);
    const merged: MemoryNote = existing
      ? {
          ...existing,
          ...note,
          tags: unique([...existing.tags, ...note.tags]),
          source_trace_ids: unique([...existing.source_trace_ids, ...note.source_trace_ids]),
          evidence_refs: unique([...existing.evidence_refs, ...note.evidence_refs]),
          utility: Math.max(existing.utility, note.utility),
          confidence: Math.max(existing.confidence, note.confidence),
          created_at: existing.created_at,
          updated_at: note.updated_at,
        }
      : { ...note, tags: unique(note.tags), source_trace_ids: unique(note.source_trace_ids), evidence_refs: unique(note.evidence_refs) };
    this.notes.set(merged.note_id, merged);
    return merged;
  }

  list(): MemoryNote[] {
    return [...this.notes.values()].sort((a, b) => a.note_id.localeCompare(b.note_id));
  }
}

function buildEpisodicNote(trace: EvolutionTrace, createdAt: string): MemoryNote {
  const contamination = contaminationReport(trace.steps);
  return {
    note_id: `mem:episodic:${trace.trace_id}`,
    tier: "episodic",
    title: trace.objective,
    body: `${trace.domain} trace ${trace.trace_id} ended as ${trace.outcome}; steps=${trace.steps.length}; high-value actions=${successfulActions(trace.steps).join(" | ")}`,
    tags: unique([trace.domain, trace.outcome, "trace", ...trace.tags]),
    created_at: createdAt,
    updated_at: createdAt,
    source_trace_ids: [trace.trace_id],
    evidence_refs: trace.steps.map((step) => step.step_id),
    utility: round(traceUtility(trace)),
    confidence: confidenceFromTrace(trace, contamination),
    contamination,
    metadata: { outcome: trace.outcome, steps: trace.steps.length },
  };
}

function buildProceduralNotes(trace: EvolutionTrace, createdAt: string, minReward: number): MemoryNote[] {
  return trace.steps
    .filter((step) => step.verifier_pass && step.branch_coverage_delta >= minReward && step.unsafe_actions === 0)
    .map((step) => {
      const contamination = contaminationReport([step]);
      const key = hashJson([trace.domain, step.kind, step.action]).slice(0, 16);
      return {
        note_id: `mem:procedural:${key}`,
        tier: "procedural",
        title: `When ${step.kind}, prefer: ${step.action}`,
        body: `Reusable procedure from ${trace.trace_id}: ${step.action}. It improved branch coverage by ${step.branch_coverage_delta} with token_cost=${step.token_cost}.`,
        tags: unique([trace.domain, step.kind, "procedure", ...trace.tags]),
        created_at: createdAt,
        updated_at: createdAt,
        source_trace_ids: [trace.trace_id],
        evidence_refs: [step.step_id],
        utility: round(step.branch_coverage_delta - step.token_cost / 5000 - step.latency_ms / 10000),
        confidence: confidenceFromStep(step),
        contamination,
        metadata: { kind: step.kind, token_cost: step.token_cost, latency_ms: step.latency_ms },
      };
    });
}

function buildSemanticNotes(trace: EvolutionTrace, createdAt: string): MemoryNote[] {
  const tags = unique([trace.domain, ...trace.tags]);
  if (tags.length === 0) return [];
  const contamination = contaminationReport(trace.steps);
  return [
    {
      note_id: `mem:semantic:${hashJson([trace.domain, tags]).slice(0, 16)}`,
      tier: "semantic",
      title: `${trace.domain} stable task context`,
      body: `Observed tags ${tags.join(", ")} in ${trace.trace_id}; outcome=${trace.outcome}; verifier_failures=${contamination.verifier_failures}.`,
      tags: unique([...tags, "semantic"]),
      created_at: createdAt,
      updated_at: createdAt,
      source_trace_ids: [trace.trace_id],
      evidence_refs: trace.steps.map((step) => step.step_id),
      utility: round(traceUtility(trace) / Math.max(1, trace.steps.length)),
      confidence: contamination.contaminated ? 0.35 : 0.7,
      contamination,
      metadata: { domain: trace.domain },
    },
  ];
}

function contaminationReport(steps: EvolutionTraceStep[]): MemoryContaminationReport {
  const unsupportedClaims = steps.reduce((sum, step) => sum + step.unsupported_claims, 0);
  const unsafeActions = steps.reduce((sum, step) => sum + step.unsafe_actions, 0);
  const verifierFailures = steps.filter((step) => !step.verifier_pass).length;
  const reasons = [
    ...(unsupportedClaims > 0 ? [`unsupported_claims:${unsupportedClaims}`] : []),
    ...(unsafeActions > 0 ? [`unsafe_actions:${unsafeActions}`] : []),
    ...(verifierFailures > 0 ? [`verifier_failures:${verifierFailures}`] : []),
  ];
  return {
    contaminated: reasons.length > 0,
    reasons,
    unsupported_claims: unsupportedClaims,
    unsafe_actions: unsafeActions,
    verifier_failures: verifierFailures,
  };
}

function successfulActions(steps: EvolutionTraceStep[]): string[] {
  return steps.filter((step) => step.verifier_pass && step.unsafe_actions === 0).map((step) => step.action).slice(0, 5);
}

function traceUtility(trace: EvolutionTrace): number {
  const outcomeBonus = trace.outcome === "accepted" ? 1 : trace.outcome === "failed" ? -1 : 0;
  const stepScore = trace.steps.reduce(
    (sum, step) =>
      sum +
      step.branch_coverage_delta +
      (step.verifier_pass ? 0.25 : -0.4) +
      (step.memory_reuse_success ? 0.15 : 0) -
      step.unsupported_claims * 0.5 -
      step.unsafe_actions,
    0,
  );
  return outcomeBonus + stepScore;
}

function confidenceFromTrace(trace: EvolutionTrace, contamination: MemoryContaminationReport): number {
  const passRate = trace.steps.length === 0 ? 0 : trace.steps.filter((step) => step.verifier_pass).length / trace.steps.length;
  const penalty = contamination.unsupported_claims * 0.12 + contamination.unsafe_actions * 0.3;
  return clamp(round(passRate + (trace.outcome === "accepted" ? 0.2 : 0) - penalty), 0, 1);
}

function confidenceFromStep(step: EvolutionTraceStep): number {
  return clamp(round(0.55 + step.branch_coverage_delta + (step.memory_reuse_success ? 0.1 : 0) - step.token_cost / 10000), 0, 1);
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort();
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
