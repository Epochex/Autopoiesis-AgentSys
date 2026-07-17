"""Tests for the OPTIONAL cross-encoder reranker baseline (core.eval.reranker).

Every test skips cleanly when the ``rerank`` extra (sentence-transformers) is absent,
so the default system-python suite stays green. Run inside ``.venv-dense`` to exercise
the real cross-encoder path.

Kept cheap and hermetic: the pure two-stage plumbing (metrics, delta table, BEIR TSV/
JSONL parsing, deterministic tie-breaks) is tested without a model or the network. The
one test that actually loads the cross-encoder is marked and skips if the ~80MB model
is not already cached locally, so CI never downloads a model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the whole module unless the reranker stack is installed.
pytest.importorskip("sentence_transformers")

from core.eval import reranker as RR  # noqa: E402


# ── pure two-stage plumbing (no model, no network) ───────────────────────────────
class _StubReranker:
    """A deterministic fake cross-encoder: scores by a fixed per-id preference map."""

    def __init__(self, prefs: dict[str, float]):
        self.prefs = prefs

    def rerank(self, query_text, candidates, top_k):
        scored = [(cid, self.prefs.get(cid, 0.0)) for cid, _ in candidates]
        scored.sort(key=lambda p: (-p[1], p[0]))
        return [cid for cid, _ in scored[:top_k]]


def test_two_stage_driver_reranks_and_scores():
    # first stage returns d3,d1,d2 (relevant d1); a reranker that prefers d1 lifts recall@1.
    queries = [("q", {"d1"})]
    first_stage = lambda q, k: ["d3", "d1", "d2"][:k]
    docs = {"d1": "t1", "d2": "t2", "d3": "t3"}
    res = RR._eval_first_stage_then_rerank(
        queries=queries,
        first_stage=first_stage,
        doc_text_of=lambda cid: docs[cid],
        reranker=_StubReranker({"d1": 9.0, "d3": 1.0, "d2": 0.5}),
        rerank_depth=3,
        k_values=(1, 3),
    )
    assert res["first_stage"][1]["recall_at_k"] == 0.0     # d3 first, miss @1
    assert res["reranked"][1]["recall_at_k"] == 1.0        # rerank puts d1 first
    assert res["pool_recall"] == 1.0                       # first stage did surface d1


def test_delta_table_signs():
    res = {
        "first_stage": {10: {"recall_at_k": 0.20, "ndcg_at_k": 0.30}},
        "reranked": {10: {"recall_at_k": 0.25, "ndcg_at_k": 0.27}},
    }
    d = RR._delta_table(res, (10,))
    assert d["recall_at_k@10"]["abs_delta"] == pytest.approx(0.05)
    assert d["recall_at_k@10"]["rel_delta_pct"] == pytest.approx(25.0)
    assert d["ndcg_at_k@10"]["abs_delta"] == pytest.approx(-0.03)  # honest: rerank can hurt


def test_rerank_is_deterministic_tie_break_on_id():
    # equal scores must resolve in id order, never arbitrarily.
    r = _StubReranker({"b": 1.0, "a": 1.0, "c": 1.0})
    out = r.rerank("q", [("b", "x"), ("a", "y"), ("c", "z")], 2)
    assert out == ["a", "b"]


# ── BEIR loader parsing (no network: write a tiny fixture into the cache layout) ──
def test_load_beir_parses_local_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(RR, "_CACHE_DIR", tmp_path)
    root = tmp_path / "beir" / "toyset"
    (root / "qrels").mkdir(parents=True)
    (root / "corpus.jsonl").write_text(
        json.dumps({"_id": "c1", "title": "Alpha", "text": "first doc"}) + "\n"
        + json.dumps({"_id": "c2", "title": "", "text": "second doc"}) + "\n",
        encoding="utf-8",
    )
    (root / "queries.jsonl").write_text(
        json.dumps({"_id": "q1", "text": "find alpha"}) + "\n"
        + json.dumps({"_id": "q2", "text": "unjudged query"}) + "\n",
        encoding="utf-8",
    )
    (root / "qrels" / "test.tsv").write_text("query-id\tcorpus-id\tscore\nq1\tc1\t1\n", encoding="utf-8")

    data = RR.load_beir("toyset", "test")
    assert data["corpus"]["c1"] == "Alpha first doc"
    assert data["corpus"]["c2"] == "second doc"          # empty title trimmed
    assert data["qrels"] == {"q1": {"c1"}}
    assert set(data["queries"]) == {"q1"}                 # only judged queries kept


def test_download_beir_returns_cached_without_network(tmp_path, monkeypatch):
    # if the dataset is already extracted, _download_beir must not touch the network.
    monkeypatch.setattr(RR, "_CACHE_DIR", tmp_path)
    root = tmp_path / "beir" / "toyset"
    (root / "qrels").mkdir(parents=True)
    (root / "corpus.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "queries.jsonl").write_text("{}\n", encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("network must not be used when cache exists")

    monkeypatch.setattr(RR.urllib.request, "urlopen", _boom)
    assert RR._download_beir("toyset") == root


# ── real cross-encoder (only if the model is already cached; never downloads in CI) ─
def _model_is_cached(model_name: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    try:
        hit = try_to_load_from_cache(model_name, "config.json")
        return isinstance(hit, str)
    except Exception:
        return False


@pytest.mark.skipif(
    not _model_is_cached(RR.DEFAULT_RERANKER),
    reason="cross-encoder model not cached locally; skip to avoid a network download",
)
def test_real_cross_encoder_ranks_relevant_first():
    r = RR.CrossEncoderReranker(RR.DEFAULT_RERANKER)
    q = "what is the capital of france?"
    cands = [
        ("irrelevant", "Bananas are a yellow tropical fruit."),
        ("relevant", "Paris is the capital and most populous city of France."),
    ]
    out = r.rerank(q, cands, 2)
    assert out[0] == "relevant"
