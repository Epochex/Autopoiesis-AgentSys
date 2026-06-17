import type { MemoryHit, MemoryNote, MemoryNoteQuery } from "./types.js";

export function retrieveMemoryNotes(notes: MemoryNote[], query: MemoryNoteQuery): MemoryHit[] {
  const queryTerms = tokenize(query.text);
  const queryTags = new Set(query.tags ?? []);
  const tiers = new Set(query.tiers ?? ["episodic", "semantic", "procedural"]);
  const minConfidence = query.minConfidence ?? 0;
  return notes
    .filter((note) => tiers.has(note.tier))
    .filter((note) => (query.includeContaminated ? true : !note.contamination.contaminated))
    .filter((note) => note.confidence >= minConfidence)
    .map((note) => scoreNote(note, queryTerms, queryTags))
    .filter((hit) => hit.score > 0)
    .sort((a, b) => b.score - a.score || b.note.updated_at.localeCompare(a.note.updated_at) || a.note.note_id.localeCompare(b.note.note_id))
    .slice(0, query.limit ?? 8);
}

function scoreNote(note: MemoryNote, queryTerms: Set<string>, queryTags: Set<string>): MemoryHit {
  const noteTerms = tokenize(`${note.title} ${note.body} ${note.tags.join(" ")}`);
  const termHits = [...queryTerms].filter((term) => noteTerms.has(term));
  const matchedTags = note.tags.filter((tag) => queryTags.has(tag));
  const requestedTierBoost = queryTerms.has(note.tier) ? 2.5 : 0;
  const tierBoost = note.tier === "procedural" ? 1.2 : note.tier === "episodic" ? 0.8 : 0.5;
  const confidenceBoost = note.confidence * 2;
  const utilityBoost = Math.max(-1, Math.min(2, note.utility));
  const contaminationPenalty = note.contamination.contaminated ? -4 : 0;
  const score = termHits.length + matchedTags.length * 1.5 + requestedTierBoost + tierBoost + confidenceBoost + utilityBoost + contaminationPenalty;
  return {
    note,
    score: round(score),
    matched_tags: matchedTags,
    reasons: [
      ...(termHits.length > 0 ? [`term_hits:${termHits.length}`] : []),
      ...(matchedTags.length > 0 ? [`tag_hits:${matchedTags.join(",")}`] : []),
      `tier:${note.tier}`,
      `confidence:${note.confidence}`,
    ],
  };
}

function tokenize(value: string): Set<string> {
  return new Set(value.toLowerCase().split(/[^a-z0-9_:-]+/).filter((term) => term.length > 1));
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
