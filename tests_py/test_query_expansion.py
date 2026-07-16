"""Tests for deterministic query expansion + its lift on the real held-out set."""
from __future__ import annotations

from core.memory.query_expansion import stem, stem_tokens, expand_tokens, make_transform
from core.eval.skill_retrieval import run_skill_retrieval_eval


def test_stem_strips_inflection_symmetrically():
    assert stem("denying") == "deny"      # the failing-case bridge
    assert stem("flows") == "flow"
    # deliberately crude: strips "es" wholesale, so failures->failur. That is fine
    # because it is applied symmetrically to both query and documents.
    assert stem("failures") == "failur"


def test_stem_guards_short_tokens():
    # never truncate a short token into noise.
    assert stem("is") == "is"
    assert stem("des") == "des"


def test_expand_adds_stemmed_synonyms_query_side():
    out = expand_tokens(["flows"])          # flow -> +traffic
    assert "flow" in out and "traffic" in out


def test_expand_is_deterministic_and_order_stable():
    assert expand_tokens(["deny", "flows"]) == expand_tokens(["deny", "flows"])


def test_make_transform_modes():
    q, d = make_transform("stem")
    assert q(["flows"]) == ["flow"] and d(["flows"]) == ["flow"]
    import pytest
    with pytest.raises(ValueError):
        make_transform("nope")


def test_stemming_lifts_real_heldout_recall():
    # the honest, no-domain-lexicon result: symmetric stemming > raw.
    base = run_skill_retrieval_eval(mode="base")["methods"]["rrf"][3]["recall_at_k"]
    stemmed = run_skill_retrieval_eval(mode="stem")["methods"]["rrf"][3]["recall_at_k"]
    assert stemmed > base
    assert round(base, 3) == 0.833 and round(stemmed, 3) == 0.917


def test_expand_reaches_full_recall_on_heldout():
    expanded = run_skill_retrieval_eval(mode="expand")["methods"]["rrf"][3]["recall_at_k"]
    assert round(expanded, 3) == 1.0
