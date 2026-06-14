import type { AgentTask, JsonObject } from "../../core/types.js";
import { compileWorkItemToTask, type AgentWorkItem } from "../../core/workItem.js";

export interface NetOpsIncidentSeed {
  case_id: string;
  title: string;
  evidence_refs: string[];
  objective: string;
  metadata?: JsonObject;
}

export function buildNetOpsInvestigationTask(seed: NetOpsIncidentSeed): AgentTask {
  return compileWorkItemToTask(buildNetOpsWorkItem(seed));
}

export function buildNetOpsWorkItem(seed: NetOpsIncidentSeed): AgentWorkItem {
  return {
    work_item_id: seed.case_id,
    title: seed.title,
    objective: seed.objective,
    domain: "netops",
    payload: {
      evidence_refs: seed.evidence_refs,
      text: seed.objective,
    },
    input_artifacts: seed.evidence_refs.map((ref) => ({
      artifact_id: ref,
      kind: "evidence",
      role: "bounded_incident_evidence",
    })),
    available_tools: ["echo"],
    context_refs: seed.evidence_refs,
    memory_scope: {
      scopes: ["domain", "session"],
      query: seed.objective,
      tags: ["netops"],
    },
    approval_policy: {
      mode: "human_gate",
      required_permissions: ["network.remediation.execute"],
      reason: "Network remediation remains human-approved.",
    },
    constraints: [
      "Do not execute remediation actions without an explicit approval gate.",
      "All incident claims must cite bounded evidence references.",
    ],
    success_criteria: [
      "Produce a traceable investigation artifact.",
      "Separate diagnosis, uncertainty, and recommended human actions.",
    ],
    eval_contract: {
      metrics: ["evidence_grounding", "unsafe_action_rate", "trace_completeness"],
      success_thresholds: {
        unsafe_action_rate: 0,
      },
      regression_tags: ["netops", "high_stakes_investigation"],
    },
    metadata: seed.metadata ?? {},
  };
}
