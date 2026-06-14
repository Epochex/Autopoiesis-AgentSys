import { createHash } from "node:crypto";
import type { ContextBudget, ContextPack, MemoryQuery, MemoryStore } from "./types.js";

export async function buildContextPack(store: MemoryStore, query: MemoryQuery, budget: ContextBudget): Promise<ContextPack> {
  const memories = await store.search({ ...query, limit: Math.max(query.limit ?? budget.maxItems, budget.maxItems) });
  const selected = memories.slice(0, budget.maxItems);
  let compressed = selected.map((item) => `- [${item.record.scope}] ${item.record.subject}: ${item.record.content}`).join("\n");
  let dropped = memories.length - selected.length;
  if (compressed.length > budget.maxChars) {
    compressed = compressed.slice(0, budget.maxChars);
    dropped += Math.max(0, selected.length - compressed.split("\n").filter(Boolean).length);
  }
  if (budget.maxTokens !== undefined && estimateContextTokens(compressed) > budget.maxTokens) {
    compressed = trimToTokenBudget(compressed, budget.maxTokens);
    dropped += Math.max(0, selected.length - compressed.split("\n").filter(Boolean).length);
  }
  const tokenEstimate = estimateContextTokens(compressed);
  return {
    context_id: `ctx_${hashJson({ query, budget, selected: selected.map((item) => item.record.memory_id) }).slice(0, 16)}`,
    query: query.text,
    budget,
    memories: selected,
    compressed,
    token_estimate: tokenEstimate,
    budget_utilization: {
      items: selected.length,
      chars: compressed.length,
      tokens: tokenEstimate,
    },
    dropped,
  };
}

export function estimateContextTokens(value: string): number {
  const normalized = value.trim();
  if (!normalized) return 0;
  const whitespacePieces = normalized.split(/\s+/).filter(Boolean).length;
  const charPieces = Math.ceil(normalized.length / 4);
  return Math.max(whitespacePieces, charPieces);
}

function trimToTokenBudget(value: string, maxTokens: number): string {
  let candidate = value;
  while (estimateContextTokens(candidate) > maxTokens && candidate.length > 0) {
    const ratio = maxTokens / estimateContextTokens(candidate);
    candidate = candidate.slice(0, Math.max(0, Math.floor(candidate.length * ratio) - 1)).trimEnd();
  }
  return candidate;
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
