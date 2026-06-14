import type { AgentTask, JsonObject } from "../../core/types.js";
import { compileWorkItemToTask, type AgentWorkItem } from "../../core/workItem.js";

export interface OfficeWorkflowSeed {
  task_id: string;
  title: string;
  brief: string;
  sections: string[];
  audience?: string;
  metadata?: JsonObject;
}

export function buildOfficeWorkflowTask(seed: OfficeWorkflowSeed): AgentTask {
  return compileWorkItemToTask(buildOfficeWorkItem(seed));
}

export function buildOfficeWorkItem(seed: OfficeWorkflowSeed): AgentWorkItem {
  return {
    work_item_id: seed.task_id,
    title: seed.title,
    objective: seed.brief,
    domain: "office",
    payload: {
      brief: seed.brief,
      sections: seed.sections,
      audience: seed.audience ?? "team",
    },
    input_artifacts: [
      {
        artifact_id: `brief:${seed.task_id}`,
        kind: "document",
        role: "brief",
        media_type: "text/plain",
      },
    ],
    available_tools: ["document.compose"],
    context_refs: [],
    memory_scope: {
      scopes: ["session", "user", "domain"],
      query: seed.brief,
      tags: ["office"],
    },
    approval_policy: {
      mode: "none",
    },
    constraints: [
      "Produce a structured artifact that can be handed off asynchronously.",
      "Keep assumptions explicit and avoid pretending external tools were updated.",
    ],
    success_criteria: [
      "Draft contains all requested sections.",
      "Draft includes owners or next actions when appropriate.",
      "Output remains traceable to the task brief.",
    ],
    eval_contract: {
      metrics: ["section_coverage", "handoff_completeness", "assumption_clarity"],
      regression_tags: ["office", "digital_employee"],
    },
    metadata: seed.metadata ?? {},
  };
}
