"""Reciprocal Rank Fusion (RRF) — pure Python, zero dependencies.

Fuses several ranked lists into one by summing ``1 / (k + rank)`` across the
lists a document appears in (rank 1-based). It needs no score calibration between
the routes — only their orderings — which is why it is the standard way to combine
a lexical (BM25) ranking with a structured/tag ranking here.

Reference: Cormack, Clarke & Buettcher, 2009, "Reciprocal Rank Fusion Outperforms
Condorcet and Individual Rank Learning Methods." Default k=60 is theirs.
Deterministic: ties break on document id.
"""
from __future__ import annotations


def rrf_fuse(rankings: list[list[str]], k: int, *, c: int = 60) -> list[str]:
    """Fuse ranked id-lists into a single top-``k`` ordering.

    ``rankings`` is a list of ranked lists (each already ordered best-first). A
    document absent from a list simply contributes nothing from that route. Only
    documents appearing in at least one list can be returned, so if every route
    abstains the result is empty.
    """
    if k <= 0:
        return []
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (c + rank)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [doc_id for doc_id, _ in fused[:k]]
