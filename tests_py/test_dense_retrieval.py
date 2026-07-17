"""Tests for the OPTIONAL dense/embedding retrieval baseline (core.eval.dense_retrieval).

Every test here skips cleanly when the ``dense`` extra is absent, so the default
system-python suite (which has no sentence-transformers / faiss / torch) stays green.
Run them inside the dedicated ``.venv-dense`` to actually exercise the embedding path.

Kept fast: the small drivers (topo 13 docs, skill 9 docs, synthetic 20 docs) run;
the 8542-doc IODA embed is NOT exercised here (only its pure, no-leakage query
renderer is), so no test downloads-and-embeds the whole pool.
"""
from __future__ import annotations

import pytest

# Skip the entire module unless the optional dense stack is installed.
pytest.importorskip("sentence_transformers")
pytest.importorskip("faiss")
pytest.importorskip("numpy")

from core.eval import dense_retrieval as D  # noqa: E402


# ── pure helpers (no model needed, but module still gated above) ─────────────────
def test_score_ranking_metrics():
    s = D.score_ranking(["a", "b", "c"], {"a", "c"}, 3)
    assert s["recall_at_k"] == 1.0
    assert round(s["precision_at_k"], 4) == round(2 / 3, 4)
    assert s["false_retrieval"] == pytest.approx(1 / 3)
    # perfect ranking (all relevant first) -> nDCG 1.0
    assert D.score_ranking(["a", "c", "x"], {"a", "c"}, 3)["ndcg_at_k"] == pytest.approx(1.0)
    # a relevant doc lower in the list lowers nDCG below 1.0
    assert D.score_ranking(["x", "a"], {"a"}, 2)["ndcg_at_k"] < 1.0


def test_ioda_query_text_is_non_leaking():
    # HONESTY: the dense query text must be built from operator-observable fields only,
    # never from an id / label / the time window.
    event = {
        "event_id": "radar:225", "radar_event_id": "9925", "locations": ["GM"], "asns": [],
        "outage_type": "NATIONWIDE", "outage_cause": "CABLE_CUT", "ioda_v2_datasources": [],
        "event_start": "2022-01-05T01:00:00+00:00", "event_end": "2022-01-05T06:00:00+00:00",
        "description": "secret leak marker",
    }
    text = D._ioda_query_text(event).lower()
    assert "gm" in text and "cable cut" in text and "nationwide" in text
    for banned in ("radar:225", "225", "9925", "2022-01-05", "secret", "01:00:00"):
        assert banned not in text, f"leaked {banned!r} into query text"


# ── faiss index behaviour (needs numpy, no model download) ───────────────────────
def _toy_embeddings(n: int = 20, dim: int = 32):
    import numpy as np

    rng = np.random.default_rng(0)
    v = rng.standard_normal((n, dim)).astype("float32")
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
    ids = [f"d{i}" for i in range(n)]
    return ids, v


def test_flat_index_finds_exact_self_as_top1():
    import numpy as np

    ids, v = _toy_embeddings()
    idx = D.DenseIndex(ids, v, "flat")
    # querying with a stored vector must return that vector first (cosine == 1).
    res = idx.search_embeddings(v[3:4], 3)[0]
    assert res[0][0] == "d3"
    assert res[0][1] == pytest.approx(1.0, abs=1e-4)


def test_hnsw_agrees_with_flat_on_top1():
    ids, v = _toy_embeddings()
    flat = D.DenseIndex(ids, v, "flat")
    hnsw = D.DenseIndex(ids, v, "hnsw")
    for i in range(len(ids)):
        top_flat = flat.search_embeddings(v[i:i + 1], 1)[0][0][0]
        top_hnsw = hnsw.search_embeddings(v[i:i + 1], 1)[0][0][0]
        assert top_flat == top_hnsw == ids[i]


def test_binary_index_runs_and_ranks_self_high():
    ids, v = _toy_embeddings()
    b = D.DenseIndex(ids, v, "binary")
    res = b.search_embeddings(v[5:6], 5)[0]
    # binary is lossy but a vector's own sign pattern has Hamming distance 0 -> top-1.
    assert res[0][0] == "d5"


def test_binary_memory_reduction_is_32x():
    ids, v = _toy_embeddings(dim=384)
    flat = D.DenseIndex(ids, v, "flat")
    binary = D.DenseIndex(ids, v, "binary")
    # float32 = 32 bits/dim, sign-bit = 1 bit/dim -> exactly 32x on the raw vectors.
    assert flat.vector_bytes() / binary.vector_bytes() == pytest.approx(32.0)


def test_ranking_is_deterministic_tie_break_on_id():
    import numpy as np

    # two identical vectors must return in id order, not arbitrary.
    v = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype="float32")
    idx = D.DenseIndex(["b", "a", "c"], v, "flat")
    res = [d for d, _ in idx.search_embeddings(v[0:1], 2)[0]]
    assert res == ["a", "b"]


# ── small dataset drivers (real fixtures; skip if the fixture data is absent) ─────
def test_topo_driver_runs_and_logical_beats_dense():
    import json
    from pathlib import Path

    if not Path("domains/network_rca/fixtures/topo_incidents.json").exists():
        pytest.skip("topo fixture absent")
    res = D.run_topo_dense_comparison()
    assert res["n_queries"] >= 3
    for m in ("logical", "naive", "dense-flat"):
        r = res["methods"][m]
        assert 0.0 <= r["recall_at_k"] <= 1.0
    # graph/logical retrieval beats dense on the multi-hop topology fixture.
    assert res["methods"]["logical"]["recall_at_k"] >= res["methods"]["dense-flat"]["recall_at_k"]


def test_skill_driver_runs_and_is_bounded():
    from pathlib import Path

    if not Path("domains/network_rca/fixtures/real/heldout_cases.json").exists():
        pytest.skip("real FortiGate held-out fixture absent")
    res = D.run_skill_dense_comparison()
    assert res["n_queries"] == 6
    assert "dense-flat" in res["methods"]
    for k in res["k_values"]:
        for m in res["methods"]:
            row = res["methods"][m][k]
            assert 0.0 <= row["recall_at_k"] <= 1.0
            assert 0.0 <= row["ndcg_at_k"] <= 1.0
