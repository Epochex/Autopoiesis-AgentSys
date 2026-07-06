"""LongMemEval conformance harness — runs and scores the memory layer.

These tests exercise the harness on the SYNTHETIC fixture only (clearly labelled);
they prove the machinery works. Real LongMemEval numbers require the real dataset:
    python -m core.eval.longmemeval /path/to/longmemeval_s.json
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.eval.longmemeval import load_longmemeval, run_longmemeval

_FIXTURE = Path(__file__).parent / "fixtures" / "longmemeval_synthetic.json"


def test_harness_runs_and_recalls_answer_sessions_on_synthetic():
    items = load_longmemeval(_FIXTURE)
    res = run_longmemeval(items, k=3)
    # 5 items, 4 answerable + 1 abstention (no answer session, excluded from recall)
    assert res["n"] == 5 and res["scored"] == 4
    # tiered retrieval should pull the answer-bearing session out of the distractors
    assert res["recall_at_k"] >= 0.75, res
    # every ability type is represented in the breakdown
    assert set(res["by_type"]) >= {"single-session-user", "multi-session", "temporal-reasoning", "knowledge-update"}


def test_missing_dataset_raises_with_download_instructions():
    with pytest.raises(FileNotFoundError, match="LongMemEval"):
        load_longmemeval("/nonexistent/longmemeval_s.json")


def test_knowledge_update_prefers_the_updated_session():
    """A knowledge-update question must retrieve the session holding the NEW fact."""
    items = load_longmemeval(_FIXTURE)
    update = next(i for i in items if i["question_type"] == "knowledge-update")
    res = run_longmemeval([update], k=2)
    assert res["recall_at_k"] == 1.0, res
