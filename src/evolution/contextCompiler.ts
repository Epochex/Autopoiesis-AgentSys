import { createHash } from "node:crypto";
import type {
  CompiledContextPacket,
  ContextEvidenceItem,
  ContextPacketBudget,
  MemoryRetrievalResult,
} from "./types.js";

export function compileContextPacket(input: {
  objective: string;
  evidence: ContextEvidenceItem[];
  memory: MemoryRetrievalResult[];
  budget: ContextPacketBudget;
  missing?: string[];
}): CompiledContextPacket {
  const requiredBranches = new Set(input.budget.required_branches ?? branchUniverse(input.evidence));
  const selected: ContextEvidenceItem[] = [];
  const excluded = new Set(input.evidence.map((item) => item.evidence_id));
  const covered = new Set<string>();
  let tokens = 0;

  const candidates = [...input.evidence].sort((a, b) => scoreEvidence(b, covered, requiredBranches) - scoreEvidence(a, covered, requiredBranches) || a.evidence_id.localeCompare(b.evidence_id));
  for (const candidate of candidates) {
    if (selected.length >= input.budget.max_items) continue;
    if (tokens + candidate.token_estimate > input.budget.max_tokens && selected.length > 0) continue;
    selected.push(candidate);
    excluded.delete(candidate.evidence_id);
    tokens += candidate.token_estimate;
    for (const branch of candidate.branch) covered.add(branch);
  }

  const excludedItems = input.evidence.filter((item) => excluded.has(item.evidence_id));
  const uncovered = [...requiredBranches].filter((branch) => !covered.has(branch)).sort();
  const branchCoverage = requiredBranches.size === 0 ? 1 : (requiredBranches.size - uncovered.length) / requiredBranches.size;
  const packetId = `ctxpkt_${hashJson({
    objective: input.objective,
    selected: selected.map((item) => item.evidence_id),
    memory: input.memory.map((item) => item.node.node_id),
    budget: input.budget,
  }).slice(0, 16)}`;

  return {
    packet_id: packetId,
    objective: input.objective,
    selected,
    excluded: excludedItems,
    missing: [...(input.missing ?? []), ...uncovered.map((branch) => `branch:${branch}`)].sort(),
    memory_refs: input.memory,
    branch_coverage: round(branchCoverage),
    token_estimate: tokens,
    budget: input.budget,
    audit: {
      selected_ids: selected.map((item) => item.evidence_id),
      excluded_ids: excludedItems.map((item) => item.evidence_id),
      covered_branches: [...covered].sort(),
      uncovered_branches: uncovered,
      reasons: [
        `selected ${selected.length}/${input.evidence.length} evidence items`,
        `estimated ${tokens}/${input.budget.max_tokens} tokens`,
        `branch coverage ${round(branchCoverage)}`,
      ],
    },
  };
}

function scoreEvidence(item: ContextEvidenceItem, covered: Set<string>, required: Set<string>): number {
  const newBranches = item.branch.filter((branch) => required.has(branch) && !covered.has(branch)).length;
  return newBranches * 10 + item.risk * 3 - item.token_estimate / 1000;
}

function branchUniverse(evidence: ContextEvidenceItem[]): string[] {
  return [...new Set(evidence.flatMap((item) => item.branch))].sort();
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
