from __future__ import annotations

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
