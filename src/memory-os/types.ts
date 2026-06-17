import type { JsonObject } from "../core/types.js";
import type { EvolutionTrace } from "../evolution/types.js";

export type MemoryTier = "episodic" | "semantic" | "procedural";

export interface MemoryNote {
  note_id: string;
  tier: MemoryTier;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
  source_trace_ids: string[];
  evidence_refs: string[];
  utility: number;
  confidence: number;
  contamination: MemoryContaminationReport;
  metadata?: JsonObject;
}

export interface MemoryContaminationReport {
  contaminated: boolean;
  reasons: string[];
  unsupported_claims: number;
  unsafe_actions: number;
  verifier_failures: number;
}

export interface MemoryNoteQuery {
  text: string;
  tags?: string[];
  tiers?: MemoryTier[];
  limit?: number;
  minConfidence?: number;
  includeContaminated?: boolean;
}

export interface MemoryHit {
  note: MemoryNote;
  score: number;
  matched_tags: string[];
  reasons: string[];
}

export interface TraceMemoryConsolidation {
  trace: EvolutionTrace;
  notes: MemoryNote[];
  quarantined: MemoryNote[];
}
