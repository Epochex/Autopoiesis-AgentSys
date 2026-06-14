import type { JsonObject } from "../core/types.js";

export type EvolutionOutcome = "accepted" | "rejected" | "needs_human" | "failed";

export type EvolutionStepKind =
  | "select_context"
  | "retrieve_memory"
  | "call_provider"
  | "verify"
  | "repair"
  | "escalate"
  | "stop"
  | "skill_patch";

export interface EvolutionTraceStep {
  step_id: string;
  kind: EvolutionStepKind;
  action: string;
  branch_coverage_delta: number;
  verifier_pass: boolean;
  human_accepted: boolean;
  memory_reuse_success: boolean;
  unsupported_claims: number;
  missing_evidence: number;
  token_cost: number;
  latency_ms: number;
  unsafe_actions: number;
  metadata?: JsonObject;
}

export interface EvolutionTrace {
  trace_id: string;
  objective: string;
  domain: string;
  outcome: EvolutionOutcome;
  steps: EvolutionTraceStep[];
  tags: string[];
}

export type MemoryNodeKind = "case" | "evidence" | "policy" | "verifier" | "human" | "skill" | "provider";

export interface MemoryGraphNode {
  node_id: string;
  kind: MemoryNodeKind;
  label: string;
  content: string;
  tags: string[];
  utility: number;
  provenance: string[];
  metadata?: JsonObject;
}

export interface MemoryGraphEdge {
  edge_id: string;
  source_id: string;
  target_id: string;
  relation:
    | "same_case"
    | "same_failure"
    | "supports"
    | "contradicts"
    | "human_accepted"
    | "verifier_rejected"
    | "generated_candidate"
    | string;
  weight: number;
}

export interface MemoryRetrievalResult {
  node: MemoryGraphNode;
  score: number;
  matched_tags: string[];
}

export interface ContextEvidenceItem {
  evidence_id: string;
  content: string;
  branch: string[];
  risk: number;
  token_estimate: number;
  provenance: string;
  metadata?: JsonObject;
}

export interface CompiledContextPacket {
  packet_id: string;
  objective: string;
  selected: ContextEvidenceItem[];
  excluded: ContextEvidenceItem[];
  missing: string[];
  memory_refs: MemoryRetrievalResult[];
  branch_coverage: number;
  token_estimate: number;
  budget: ContextPacketBudget;
  audit: {
    selected_ids: string[];
    excluded_ids: string[];
    covered_branches: string[];
    uncovered_branches: string[];
    reasons: string[];
  };
}

export interface ContextPacketBudget {
  max_items: number;
  max_tokens: number;
  required_branches?: string[];
}

export interface TopologySignal {
  risk: number;
  branch_coverage: number;
  verifier_rejections: number;
  provider_disagreement: number;
  memory_conflict: number;
  budget_pressure: number;
}

export interface SpecialistTopologyDecision {
  mode: "single_orchestrator" | "star" | "critic_loop" | "sparse_consensus";
  specialists: string[];
  reasons: string[];
  max_parallelism: number;
}

export interface RewardWeights {
  branch_coverage_gain: number;
  verifier_pass: number;
  human_acceptance: number;
  memory_reuse_success: number;
  unsupported_claim_penalty: number;
  missing_evidence_penalty: number;
  token_cost_penalty: number;
  latency_cost_penalty: number;
  unsafe_action_penalty: number;
}

export interface StepReward {
  step_id: string;
  reward: number;
  components: Record<string, number>;
}

export interface PolicyCandidate {
  candidate_id: string;
  policy_kind: "context_budget" | "memory_retrieval" | "provider_routing" | "topology_gate" | "stop_escalate" | "skill_patch";
  summary: string;
  reward_mean: number;
  safety_pass: boolean;
  replay_cases: number;
  patch: JsonObject;
  evidence: {
    trace_ids: string[];
    packet_ids: string[];
    topology_modes: string[];
  };
}
