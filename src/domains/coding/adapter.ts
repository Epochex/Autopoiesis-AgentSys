import type { AgentTask, JsonObject } from "../../core/types.js";
import { compileWorkItemToTask, type AgentWorkItem } from "../../core/workItem.js";

export interface CodingIssueSeed {
  task_id: string;
  title: string;
  issue: string;
  repository_path: string;
  target_paths?: string[];
  metadata?: JsonObject;
}

export function buildCodingInvestigationTask(seed: CodingIssueSeed): AgentTask {
  return compileWorkItemToTask(buildCodingWorkItem(seed));
}

export function buildCodingWorkItem(seed: CodingIssueSeed): AgentWorkItem {
  return {
    work_item_id: seed.task_id,
    title: seed.title,
    objective: seed.issue,
    domain: "coding",
    payload: {
      query: seed.issue,
      repository_path: seed.repository_path,
      target_paths: seed.target_paths ?? [],
    },
    input_artifacts: [
      {
        artifact_id: `repo:${seed.repository_path}`,
        kind: "repository",
        uri: seed.repository_path,
        role: "workspace",
      },
      ...(seed.target_paths ?? []).map((target) => ({
        artifact_id: `file:${target}`,
        kind: "source_path",
        uri: target,
        role: "target",
      })),
    ],
    available_tools: ["workspace.search"],
    context_refs: [],
    memory_scope: {
      scopes: ["session", "domain"],
      query: seed.issue,
      tags: ["coding"],
    },
    approval_policy: {
      mode: "human_gate",
      required_permissions: ["local.process.spawn"],
      reason: "Code changes and test execution require explicit approval.",
    },
    constraints: [
      "Inspect repository context before proposing code changes.",
      "Prefer small, test-backed changes with clear rollback paths.",
      "Do not run side-effecting commands unless approval is present.",
    ],
    success_criteria: [
      "Identify relevant files and symbols.",
      "Produce a traceable implementation plan.",
      "Run applicable tests through the sandbox before completion.",
    ],
    eval_contract: {
      metrics: ["relevant_file_recall", "test_pass_rate", "patch_minimality", "trace_completeness"],
      regression_tags: ["ai_coding", "workspace_inspection"],
    },
    metadata: seed.metadata ?? {},
  };
}
