import type { AgentTask, JsonObject } from "./types.js";

export type WorkItemDomain = "coding" | "office" | "decision" | "netops" | string;

export interface WorkItemArtifact {
  artifact_id: string;
  kind: "repository" | "document" | "scenario" | "evidence" | "dataset" | "tool_output" | string;
  uri?: string;
  media_type?: string;
  role?: string;
  metadata?: JsonObject;
}

export interface WorkItemMemoryScope {
  scopes: Array<"task" | "session" | "user" | "domain" | "global">;
  query?: string;
  tags?: string[];
}

export interface WorkItemApprovalPolicy {
  mode: "none" | "human_gate" | "preapproved";
  required_permissions?: string[];
  preapproved_permissions?: string[];
  reason?: string;
}

export interface WorkItemEvalContract {
  metrics: string[];
  success_thresholds?: Record<string, number>;
  regression_tags?: string[];
}

export interface AgentWorkItem {
  work_item_id: string;
  title: string;
  objective: string;
  domain: WorkItemDomain;
  payload: JsonObject;
  input_artifacts: WorkItemArtifact[];
  available_tools: string[];
  context_refs: string[];
  memory_scope?: WorkItemMemoryScope;
  approval_policy: WorkItemApprovalPolicy;
  constraints: string[];
  success_criteria: string[];
  eval_contract?: WorkItemEvalContract;
  metadata?: JsonObject;
}

export function compileWorkItemToTask(workItem: AgentWorkItem): AgentTask {
  return {
    task_id: workItem.work_item_id,
    title: workItem.title,
    objective: workItem.objective,
    domain: workItem.domain,
    input: {
      ...workItem.payload,
      work_item: workItem,
      skill_refs: workItem.available_tools,
      input_artifacts: workItem.input_artifacts,
      context_refs: workItem.context_refs,
      ...(workItem.memory_scope ? { memory_scope: workItem.memory_scope } : {}),
      approval_policy: workItem.approval_policy,
      approved_permissions: workItem.approval_policy.preapproved_permissions ?? [],
      ...(workItem.eval_contract ? { eval_contract: workItem.eval_contract } : {}),
      metadata: workItem.metadata ?? {},
    },
    constraints: workItem.constraints,
    success_criteria: workItem.success_criteria,
  };
}
