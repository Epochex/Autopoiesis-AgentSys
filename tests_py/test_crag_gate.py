"""Tests for the CRAG-style confidence gate (LLM-free, deterministic)."""
from __future__ import annotations

from core.memory.bm25 import BM25Index, tokenize
from core.memory.crag_gate import crag_gate
from core.memory.query_expansion import stem_tokens
from core.eval.skill_retrieval import build_skill_corpus


def test_correct_branch_high_score():
    d = crag_gate([("a", 5.0), ("b", 1.0)], 2, hi=3.0, lo=0.5)
    assert d.action == "correct" and d.results == ["a", "b"]


def test_ambiguous_branch_widens():
    d = crag_gate([("a", 1.0), ("b", 0.9), ("c", 0.8)], 1, hi=3.0, lo=0.5)
    assert d.action == "ambiguous" and d.results == ["a", "b"]   # expand_k defaults to 2*k


def test_incorrect_branch_abstains_on_low_score():
    d = crag_gate([("a", 0.2)], 3, hi=3.0, lo=0.5)
    assert d.action == "incorrect" and d.results == [] and d.reason == "low_confidence_abstain"


def test_empty_retrieval_is_not_observed():
    d = crag_gate([], 3, hi=3.0, lo=0.5)
    assert d.action == "incorrect" and d.reason == "not_observed"


def test_gate_abstains_on_the_vocab_mismatch_case():
    # the real point: on internal_deny_flood, base BM25 finds nothing -> gate
    # returns an explicit abstention, not a silent empty "no probes needed".
    docs, _ = build_skill_corpus()
    bm25 = BM25Index(docs)
    scored = bm25.rank_with_scores("denying a very high volume of flows from internal hosts", 3)
    decision = crag_gate(scored, 3, hi=1.0, lo=0.01)
    assert decision.action == "incorrect" and decision.results == []


def test_gate_recovers_case_once_query_is_expanded():
    # with stemming the same case now grounds -> gate no longer abstains.
    docs, _ = build_skill_corpus(stem_tokens)
    bm25 = BM25Index(docs)
    qtokens = stem_tokens(tokenize("denying a very high volume of flows from internal hosts"))
    scored = bm25.rank_with_scores("", 3, query_tokens=qtokens)
    decision = crag_gate(scored, 3, hi=1.0, lo=0.01)
    assert decision.action in ("correct", "ambiguous") and decision.results
