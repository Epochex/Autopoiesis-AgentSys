import type { JsonObject } from "../core/types.js";
import { buildContextPack } from "../context/contextPack.js";
import type { MemoryScope, MemoryStore } from "../context/types.js";
import type { Skill } from "./types.js";

export function createMemorySearchSkill(store: MemoryStore): Skill<JsonObject, JsonObject> {
  return {
    name: "memory.search",
    version: "0.1.0",
    description: "Search scoped selfevo-orchiter memory and return a compressed context pack.",
    input_schema: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string" },
        scopes: { type: "array" },
        maxItems: { type: "number" },
        maxChars: { type: "number" },
      },
    },
    output_schema: {
      type: "object",
      required: ["context_id", "compressed"],
      properties: {
        context_id: { type: "string" },
        compressed: { type: "string" },
        dropped: { type: "number" },
      },
    },
    permissions: [
      {
        permission: "memory.read",
        risk: "read_only",
        description: "Reads scoped selfevo-orchiter memory records.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      const scopes = parseScopes(invocation.input.scopes);
      const limit = typeof invocation.input.maxItems === "number" ? invocation.input.maxItems : undefined;
      const context = await buildContextPack(
        store,
        {
          text: String(invocation.input.query ?? ""),
          ...(scopes ? { scopes } : {}),
          ...(limit ? { limit } : {}),
        },
        {
          maxItems: typeof invocation.input.maxItems === "number" ? invocation.input.maxItems : 5,
          maxChars: typeof invocation.input.maxChars === "number" ? invocation.input.maxChars : 4000,
        },
      );
      return {
        status: "ok",
        output: context as unknown as JsonObject,
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:memory`,
            skill_name: "memory.search",
            summary: `Built context pack ${context.context_id} with ${context.memories.length} memories.`,
            cited_resource_refs: context.memories.map((item) => item.record.memory_id),
            data: {
              context_id: context.context_id,
              dropped: context.dropped,
            },
          },
        ],
      };
    },
  };
}

function parseScopes(value: unknown): MemoryScope[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const allowed = new Set(["task", "session", "user", "domain", "global"]);
  return value.map(String).filter((item): item is MemoryScope => allowed.has(item));
}
