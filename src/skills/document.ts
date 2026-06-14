import type { JsonObject } from "../core/types.js";
import type { Skill } from "./types.js";

export function createDocumentComposeSkill(): Skill<JsonObject, JsonObject> {
  return {
    name: "document.compose",
    version: "0.1.0",
    description: "Compose a structured handoff document from a brief and requested sections.",
    input_schema: {
      type: "object",
      required: ["brief", "sections"],
      properties: {
        brief: { type: "string" },
        sections: { type: "array" },
        audience: { type: "string" },
      },
    },
    output_schema: {
      type: "object",
      required: ["title", "sections", "markdown"],
      properties: {
        title: { type: "string" },
        sections: { type: "array" },
        markdown: { type: "string" },
      },
    },
    permissions: [
      {
        permission: "document.compose",
        risk: "read_only",
        description: "Creates an in-memory document draft without external side effects.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      const brief = String(invocation.input.brief ?? "");
      const sections = parseSections(invocation.input.sections);
      const audience = String(invocation.input.audience ?? "team");
      const title = firstSentence(brief) || "Agentic handoff";
      const renderedSections = sections.map((section) => ({
        heading: section,
        content: draftSection(section, brief, audience),
      }));
      const markdown = [`# ${title}`, "", `Audience: ${audience}`, "", ...renderedSections.flatMap((section) => [`## ${section.heading}`, "", section.content, ""])].join("\n");
      return {
        status: "ok",
        output: {
          title,
          sections: renderedSections,
          markdown,
        },
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:document`,
            skill_name: "document.compose",
            summary: `Composed ${renderedSections.length} document sections for ${audience}.`,
            cited_resource_refs: [],
            data: {
              section_count: renderedSections.length,
              audience,
            },
          },
        ],
      };
    },
  };
}

function parseSections(value: unknown): string[] {
  if (!Array.isArray(value)) return ["Summary", "Next Actions"];
  const sections = value.map(String).map((item) => item.trim()).filter(Boolean);
  return sections.length > 0 ? sections : ["Summary", "Next Actions"];
}

function firstSentence(value: string): string {
  return value.split(/[.!?]/)[0]?.trim().slice(0, 80) ?? "";
}

function draftSection(section: string, brief: string, audience: string): string {
  const normalized = section.toLowerCase();
  if (normalized.includes("summary")) return `This section summarizes the requested work for ${audience}: ${brief}`;
  if (normalized.includes("risk")) return "Risks and open questions should be reviewed before any external side effects or irreversible changes.";
  if (normalized.includes("action") || normalized.includes("next")) return "Next actions: confirm scope, assign an owner, run the relevant harness checks, and record the outcome in the trace.";
  return `Draft content for ${section}, grounded in the brief: ${brief}`;
}
