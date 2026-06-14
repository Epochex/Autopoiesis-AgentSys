import type { JsonObject } from "../core/types.js";
import type { Skill } from "./types.js";

export function createEchoSkill(): Skill<{ text: string }, { text: string; length: number }> {
  return {
    name: "echo",
    version: "0.1.0",
    description: "Read-only smoke skill used to validate skill invocation and trace plumbing.",
    input_schema: {
      type: "object",
      required: ["text"],
      properties: {
        text: { type: "string" },
      },
    },
    output_schema: {
      type: "object",
      required: ["text", "length"],
      properties: {
        text: { type: "string" },
        length: { type: "number" },
      },
    },
    permissions: [
      {
        permission: "local.read.none",
        risk: "read_only",
        description: "Does not read or write external resources.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      const text = String(invocation.input.text);
      return {
        status: "ok",
        output: { text, length: text.length },
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:1`,
            skill_name: "echo",
            summary: `Echoed ${text.length} characters.`,
            cited_resource_refs: [],
            data: { length: text.length },
          },
        ],
      };
    },
  };
}

export function isJsonObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
