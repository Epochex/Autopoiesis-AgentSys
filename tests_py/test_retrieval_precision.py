from __future__ import annotations

import pytest

from core.eval.retrieval_precision import run_retrieval_eval


def test_retrieval_eval_shows_logical_precision_and_false_retrieval_improvement():
    res = run_retrieval_eval()
    logical = res["methods"]["logical"]
    naive = res["methods"]["naive"]

    assert res["n_queries"] >= 3
    assert logical["precision_at_k"] >= naive["precision_at_k"]
    assert logical["false_retrieval"] <= naive["false_retrieval"]
    assert logical["precision_at_k"] > naive["precision_at_k"]
    assert logical["false_retrieval"] < naive["false_retrieval"]


def test_retrieval_eval_is_safe_on_zero_queries():
    """No queries → all-zero aggregates, never a division error."""
    res = run_retrieval_eval({"records": [], "queries": []})
    assert res["n_queries"] == 0
    for method in ("logical", "naive"):
        assert res["methods"][method] == {
            "precision_at_k": 0.0, "recall_at_k": 0.0, "false_retrieval": 0.0,
        }


def test_retrieval_eval_rejects_malformed_query_items():
    with pytest.raises(ValueError, match="missing required"):
        run_retrieval_eval({"records": [], "queries": [{"query": {"entities": []}}]})
