import type { JsonObject } from "../core/types.js";

export type MemoryScope = "task" | "session" | "user" | "domain" | "global";

export interface MemoryRecord {
  memory_id: string;
  scope: MemoryScope;
  subject: string;
  content: string;
  tags: string[];
  metadata: JsonObject;
  created_at: string;
  updated_at: string;
}

export interface MemoryQuery {
  text: string;
  scopes?: MemoryScope[];
  tags?: string[];
  limit?: number;
}

export interface MemorySearchResult {
  record: MemoryRecord;
  score: number;
}

export interface MemoryStore {
  put(record: Omit<MemoryRecord, "created_at" | "updated_at">): Promise<MemoryRecord>;
  search(query: MemoryQuery): Promise<MemorySearchResult[]>;
  get(memoryId: string): Promise<MemoryRecord | undefined>;
}

export interface ContextBudget {
  maxItems: number;
  maxChars: number;
  maxTokens?: number;
}

export interface ContextPack {
  context_id: string;
  query: string;
  budget: ContextBudget;
  memories: MemorySearchResult[];
  compressed: string;
  token_estimate: number;
  budget_utilization: {
    items: number;
    chars: number;
    tokens: number;
  };
  dropped: number;
}
