"""Property tests for the BM25 + RRF skill-retrieval eval (LLM-free, deterministic)."""
from __future__ import annotations

from core.memory.bm25 import BM25Index, tokenize
from core.memory.rrf import rrf_fuse
from core.eval.skill_retrieval import run_skill_retrieval_eval, build_retrievers


def test_bm25_ranks_term_bearing_doc_first():
    idx = BM25Index({
        "a": tokenize("admin login failed bruteforce lockout"),
        "b": tokenize("dhcp lease address allocation"),
        "c": tokenize("fortiguard update security rating"),
    })
    assert idx.rank("admin login failed", 1) == ["a"]


def test_bm25_empty_and_nonpositive_k():
    idx = BM25Index({"a": tokenize("admin login"), "b": tokenize("dhcp lease")})
    assert idx.rank("nothing matches here xyz", 3) == []   # zero-score docs dropped
    assert idx.rank("admin", 0) == []


def test_bm25_idf_never_negative():
    # a term in every doc must not contribute a negative score.
    idx = BM25Index({"a": ["x", "y"], "b": ["x", "z"]})
    assert all(v >= 0.0 for v in idx.idf.values())


def test_rrf_rewards_agreement_across_lists():
    # 'x' is mid in both lists; 'top1'/'top2' each lead one list only.
    fused = rrf_fuse([["top1", "x", "a"], ["top2", "x", "b"]], 1)
    assert fused == ["x"]


def test_rrf_deterministic_tie_break_on_id():
    assert rrf_fuse([["b", "a"], ["a", "b"]], 2) == ["a", "b"]


def test_rrf_nonpositive_k():
    assert rrf_fuse([["a", "b"]], 0) == []


def test_eval_runs_on_real_heldout_and_is_bounded():
    res = run_skill_retrieval_eval()
    assert res["n_queries"] == 6
    assert res["dataset_kind"] == "real-fortigate-heldout"
    for method in ("naive", "bm25", "structured", "rrf"):
        for k in (1, 2, 3):
            row = res["methods"][method][k]
            assert 0.0 <= row["recall_at_k"] <= 1.0
            assert 0.0 <= row["false_retrieval"] <= 1.0


def test_eval_is_deterministic():
    assert run_skill_retrieval_eval() == run_skill_retrieval_eval()


def test_structured_retriever_never_false_retrieves_on_heldout():
    # the honest positive result: curated-tag retrieval picks only relevant probes.
    res = run_skill_retrieval_eval()
    assert res["methods"]["structured"][3]["false_retrieval"] == 0.0
