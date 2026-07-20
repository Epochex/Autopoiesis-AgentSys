"""Tests for the reusable hybrid knowledge-base retriever.

The orchestration tests inject tiny deterministic dense/rerank adapters, so the
default system-Python suite exercises every stage without model downloads.  The
faiss-specific smoke test uses ``pytest.importorskip`` for the optional stack.
"""
from __future__ import annotations

import threading

import pytest

from core.memory.hybrid_kb import HybridKBRetriever, KBDocument


class _FixedDense:
    def __init__(self, ranking: list[str], barrier: threading.Barrier | None = None):
        self.ranking = ranking
        self.barrier = barrier
        self.thread_name = ""

    def search_texts(self, query_texts, k, *, model_name=None):
        self.thread_name = threading.current_thread().name
        if self.barrier:
            self.barrier.wait(timeout=2)
        return [[(doc_id, 1.0 / (rank + 1)) for rank, doc_id in enumerate(self.ranking[:k])]]


class _FixedReranker:
    def __init__(self, preference: list[str]):
        self.preference = preference
        self.calls = []

    def rerank(self, query_text, candidates, top_k):
        self.calls.append((query_text, list(candidates), top_k))
        available = {doc_id for doc_id, _ in candidates}
        return [doc_id for doc_id in self.preference if doc_id in available][:top_k]


def _retriever(*, dense=None, reranker=None):
    docs = [
        KBDocument("lexical", "administrator login lockout"),
        KBDocument("both", "administrator authentication policy"),
        KBDocument("semantic", "account access disabled after repeated failures"),
    ]
    return HybridKBRetriever(
        docs,
        fusion=dense is not None,
        rerank=reranker is not None,
        dense_index=dense,
        reranker_instance=reranker,
        fusion_depth=3,
        rerank_depth=3,
    )


def test_bm25_only_needs_no_optional_dependencies():
    retriever = _retriever()
    assert retriever.retrieve_ids("administrator login lockout", 2) == ["lexical", "both"]
    assert retriever.retrieve("", 2) == []
    assert retriever.retrieve("anything", 0) == []


def test_bm25_and_dense_are_searched_in_parallel_then_rrf_fused(monkeypatch):
    barrier = threading.Barrier(2)
    dense = _FixedDense(["semantic", "both", "lexical"], barrier)
    retriever = _retriever(dense=dense)
    original_rank = retriever.bm25.rank
    bm25_thread = []

    def synchronized_rank(query, k):
        bm25_thread.append(threading.current_thread().name)
        barrier.wait(timeout=2)
        return original_rank(query, k)

    monkeypatch.setattr(retriever.bm25, "rank", synchronized_rank)
    # "both" appears second in both routes and therefore outranks either route's
    # isolated top-1 result under RRF.
    assert retriever.retrieve_ids("administrator policy", 3, rerank=False)[0] == "both"
    assert bm25_thread[0].startswith("hybrid-kb")
    assert dense.thread_name.startswith("hybrid-kb")


def test_ablation_flags_and_rerank_depth_are_per_call():
    dense = _FixedDense(["semantic", "both", "lexical"])
    reranker = _FixedReranker(["semantic", "both", "lexical"])
    retriever = _retriever(dense=dense, reranker=reranker)

    assert retriever.retrieve_ids("administrator policy", 1, fusion=False, rerank=False) == ["both"]
    assert retriever.retrieve_ids("administrator policy", 1, fusion=True, rerank=False) == ["both"]
    assert retriever.retrieve_ids(
        "administrator policy", 1, fusion=True, rerank=True, rerank_depth=3,
    ) == ["semantic"]
    assert reranker.calls[-1][2] == 3
    assert len(reranker.calls[-1][1]) == 3


def test_reranker_adapter_cannot_inject_unknown_or_duplicate_documents():
    dense = _FixedDense(["semantic", "both", "lexical"])
    reranker = _FixedReranker(["missing", "semantic", "semantic"])
    retriever = _retriever(dense=dense, reranker=reranker)
    ids = retriever.retrieve_ids("administrator policy", 3)
    assert ids[0] == "semantic"
    assert "missing" not in ids
    assert len(ids) == len(set(ids)) == 3


def test_from_corpus_uses_context_header_and_preserves_metadata():
    corpus = {
        "chunks": [{
            "id": "656084#0",
            "section_id": "656084",
            "context_header": "FortiOS > Policies > Firewall policy",
            "text": "Traffic must match an accept policy.",
        }],
    }
    retriever = HybridKBRetriever.from_corpus(corpus, fusion=False, rerank=False)
    doc = retriever.documents["656084#0"]
    assert doc.text == (
        "FortiOS > Policies > Firewall policy\n\nTraffic must match an accept policy."
    )
    assert doc.metadata["section_id"] == "656084"
    assert doc.metadata["context_header"].startswith("FortiOS")


def test_from_corpus_accepts_mapping_and_rejects_duplicate_ids():
    retriever = HybridKBRetriever.from_corpus(
        {"a": "alpha text", "b": "beta text"}, fusion=False, rerank=False,
    )
    assert len(retriever) == 2
    with pytest.raises(ValueError, match="duplicate document id"):
        HybridKBRetriever(
            [KBDocument("a", "one"), KBDocument("a", "two")],
            fusion=False,
            rerank=False,
        )


def test_requesting_unbuilt_optional_stage_fails_loudly():
    retriever = _retriever()
    with pytest.raises(RuntimeError, match="no dense index"):
        retriever.retrieve("administrator", fusion=True, rerank=False)
    with pytest.raises(RuntimeError, match="no reranker"):
        retriever.retrieve("administrator", fusion=False, rerank=True)


def test_optional_faiss_hnsw_index_constructs():
    np = pytest.importorskip("numpy")
    pytest.importorskip("faiss")
    from core.eval.dense_retrieval import DenseIndex

    vectors = np.eye(3, dtype="float32")
    dense = DenseIndex(["a", "b", "c"], vectors, index_type="hnsw")
    retriever = HybridKBRetriever.from_corpus(
        {"a": "alpha", "b": "beta", "c": "gamma"},
        fusion=True,
        rerank=False,
        dense_index=dense,
    )
    assert retriever.dense_index.index_type == "hnsw"


def test_default_dense_builder_requests_hnsw(monkeypatch):
    from core.eval import dense_retrieval

    sentinel = _FixedDense(["a"])
    seen = {}

    def fake_build(doc_ids, doc_texts, **kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(dense_retrieval.DenseIndex, "build", fake_build)
    retriever = HybridKBRetriever.from_corpus(
        {"a": "alpha"}, fusion=True, rerank=False,
    )
    assert retriever.dense_index is sentinel
    assert seen["index_type"] == "hnsw"


@pytest.mark.parametrize("field,value", [("k", 0), ("rerank_depth", 0), ("fusion_depth", 0)])
def test_invalid_configuration_is_rejected(field, value):
    kwargs = {"fusion": False, "rerank": False, field: value}
    with pytest.raises(ValueError, match=field):
        HybridKBRetriever([KBDocument("a", "alpha")], **kwargs)
