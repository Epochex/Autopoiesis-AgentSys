import { createHash } from "node:crypto";
import type { EvolutionTrace, MemoryGraphEdge, MemoryGraphNode, MemoryRetrievalResult } from "./types.js";

export class EnterpriseMemoryGraph {
  private readonly nodes = new Map<string, MemoryGraphNode>();
  private readonly edges = new Map<string, MemoryGraphEdge>();

  upsertNode(node: MemoryGraphNode): MemoryGraphNode {
    const existing = this.nodes.get(node.node_id);
    const merged: MemoryGraphNode = existing
      ? {
          ...existing,
          ...node,
          tags: unique([...existing.tags, ...node.tags]),
          provenance: unique([...existing.provenance, ...node.provenance]),
          utility: Math.max(existing.utility, node.utility),
        }
      : { ...node, tags: unique(node.tags), provenance: unique(node.provenance) };
    this.nodes.set(merged.node_id, merged);
    return merged;
  }

  link(edge: Omit<MemoryGraphEdge, "edge_id"> & { edge_id?: string }): MemoryGraphEdge {
    const saved: MemoryGraphEdge = {
      ...edge,
      edge_id: edge.edge_id ?? `edge_${hashJson([edge.source_id, edge.target_id, edge.relation]).slice(0, 16)}`,
    };
    this.edges.set(saved.edge_id, saved);
    return saved;
  }

  ingestTrace(trace: EvolutionTrace): MemoryGraphNode {
    const positive = trace.outcome === "accepted";
    const verifierFailures = trace.steps.reduce((count, step) => count + (step.verifier_pass ? 0 : 1), 0);
    const utility =
      trace.steps.reduce((sum, step) => sum + step.branch_coverage_delta, 0) +
      trace.steps.filter((step) => step.human_accepted).length * 0.5 -
      verifierFailures * 0.25;
    const caseNode = this.upsertNode({
      node_id: `case:${trace.trace_id}`,
      kind: "case",
      label: trace.objective,
      content: `${trace.domain} trace ended as ${trace.outcome}`,
      tags: unique([trace.domain, trace.outcome, ...trace.tags]),
      utility,
      provenance: [trace.trace_id],
      metadata: { outcome: trace.outcome, steps: trace.steps.length },
    });

    for (const step of trace.steps) {
      const stepNode = this.upsertNode({
        node_id: `step:${trace.trace_id}:${step.step_id}`,
        kind: step.verifier_pass ? "policy" : "verifier",
        label: step.action,
        content: `${step.kind}: branch_delta=${step.branch_coverage_delta}, verifier=${step.verifier_pass}`,
        tags: unique([trace.domain, step.kind, step.verifier_pass ? "verifier_pass" : "verifier_rejected"]),
        utility: step.human_accepted || positive ? Math.max(0, step.branch_coverage_delta) : -0.2,
        provenance: [trace.trace_id, step.step_id],
        ...(step.metadata ? { metadata: step.metadata } : {}),
      });
      this.link({
        source_id: caseNode.node_id,
        target_id: stepNode.node_id,
        relation: step.verifier_pass ? "supports" : "verifier_rejected",
        weight: step.verifier_pass ? 1 : 0.4,
      });
    }
    return caseNode;
  }

  retrieve(query: { text: string; tags?: string[]; limit?: number }): MemoryRetrievalResult[] {
    const queryTerms = tokenize(query.text);
    const queryTags = new Set(query.tags ?? []);
    return [...this.nodes.values()]
      .map((node) => {
        const nodeTerms = tokenize(`${node.label} ${node.content} ${node.tags.join(" ")}`);
        const termHits = [...queryTerms].filter((term) => nodeTerms.has(term)).length;
        const matchedTags = node.tags.filter((tag) => queryTags.has(tag));
        const tagScore = matchedTags.length * 1.5;
        const utilityScore = Math.max(-1, Math.min(1, node.utility));
        return {
          node,
          score: termHits + tagScore + utilityScore,
          matched_tags: matchedTags,
        };
      })
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score || a.node.node_id.localeCompare(b.node.node_id))
      .slice(0, query.limit ?? 5);
  }

  snapshot(): { nodes: MemoryGraphNode[]; edges: MemoryGraphEdge[] } {
    return {
      nodes: [...this.nodes.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
      edges: [...this.edges.values()].sort((a, b) => a.edge_id.localeCompare(b.edge_id)),
    };
  }
}

function tokenize(value: string): Set<string> {
  return new Set(value.toLowerCase().split(/[^a-z0-9_:-]+/).filter((term) => term.length > 1));
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort();
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
