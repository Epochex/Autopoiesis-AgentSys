"""CRAG-style confidence gate over retrieval scores — LLM-free, deterministic.

Corrective RAG (Yan et al., 2024, arXiv:2401.15884) runs a lightweight evaluator
on the retrieved set and takes one of three corrective actions. This is the
model-free realisation of that idea: the evaluator is the retrieval score itself,
so no extra model or LLM call is needed and the decision is fully reproducible.

    top score ≥ hi         -> CORRECT    : use the top results as-is
    lo ≤ top score < hi    -> AMBIGUOUS  : widen the pool (expand k) and return more
    top score < lo / empty -> INCORRECT  : abstain — return nothing, reason recorded

Abstaining on low confidence is the point: on a query the retriever cannot ground
(the ``internal_deny_flood`` vocabulary-mismatch case), the gate returns an explicit
``not_observed`` abstention instead of silently emitting an empty list that reads as
"no probes needed". That mirrors the project's existing "降级为未观测" behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateDecision:
    action: str                       # "correct" | "ambiguous" | "incorrect"
    results: list[str] = field(default_factory=list)
    reason: str = ""
    top_score: float = 0.0


def crag_gate(
    scored: list[tuple[str, float]],
    k: int,
    *,
    hi: float,
    lo: float,
    expand_k: int | None = None,
) -> GateDecision:
    """Classify a scored candidate list and act (correct / ambiguous / incorrect).

    ``scored`` is ``(doc_id, score)`` best-first (e.g. ``BM25Index.rank_with_scores``).
    ``hi``/``lo`` are score thresholds; ``expand_k`` is the widened cut used on the
    ambiguous branch (defaults to ``2*k``). Deterministic — no randomness, no model.
    """
    if k <= 0:
        return GateDecision("incorrect", [], "nonpositive_k", 0.0)
    if not scored:
        return GateDecision("incorrect", [], "not_observed", 0.0)

    top_score = scored[0][1]
    if top_score >= hi:
        return GateDecision("correct", [d for d, _ in scored[:k]], "high_confidence", top_score)
    if top_score < lo:
        # confidently nothing — abstain rather than hallucinate a probe.
        return GateDecision("incorrect", [], "low_confidence_abstain", top_score)
    widened = expand_k if expand_k is not None else 2 * k
    return GateDecision("ambiguous", [d for d, _ in scored[:widened]], "expanded", top_score)
