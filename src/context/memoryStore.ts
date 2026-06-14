import { randomUUID } from "node:crypto";
import type { MemoryQuery, MemoryRecord, MemorySearchResult, MemoryStore } from "./types.js";

export class InMemoryMemoryStore implements MemoryStore {
  private readonly records = new Map<string, MemoryRecord>();

  async put(record: Omit<MemoryRecord, "created_at" | "updated_at">): Promise<MemoryRecord> {
    const now = new Date().toISOString();
    const memory: MemoryRecord = {
      ...record,
      memory_id: record.memory_id || `mem_${randomUUID().slice(0, 12)}`,
      created_at: now,
      updated_at: now,
    };
    this.records.set(memory.memory_id, memory);
    return structuredClone(memory);
  }

  async search(query: MemoryQuery): Promise<MemorySearchResult[]> {
    const terms = tokenize(query.text);
    const scopes = query.scopes ? new Set(query.scopes) : undefined;
    const tags = query.tags ? new Set(query.tags) : undefined;
    return [...this.records.values()]
      .filter((record) => !scopes || scopes.has(record.scope))
      .filter((record) => !tags || record.tags.some((tag) => tags.has(tag)))
      .map((record) => ({ record, score: scoreRecord(record, terms) }))
      .filter((result) => result.score > 0)
      .sort((left, right) => right.score - left.score)
      .slice(0, query.limit ?? 5)
      .map((result) => structuredClone(result));
  }

  async get(memoryId: string): Promise<MemoryRecord | undefined> {
    const record = this.records.get(memoryId);
    return record ? structuredClone(record) : undefined;
  }
}

function scoreRecord(record: MemoryRecord, terms: string[]): number {
  const haystack = tokenize([record.subject, record.content, record.tags.join(" ")].join(" "));
  if (terms.length === 0) return 0;
  const haystackSet = new Set(haystack);
  const overlap = terms.filter((term) => haystackSet.has(term)).length;
  return overlap / terms.length;
}

function tokenize(value: string): string[] {
  return value.toLowerCase().split(/[^a-z0-9_/-]+/).filter(Boolean);
}
