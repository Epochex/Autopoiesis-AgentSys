from __future__ import annotations

import subprocess
import sys

import pytest

from core.memory.vector_memory import BGETextEmbedder, VectorMemoryIndex


class DeterministicTestEmbedder:
    """Small explicit vector table for lifecycle tests, never a production fallback."""

    dimension = 3
    model_id = "test/explicit-vector-table"

    _vectors = {
        "bgp peer reset": [1.0, 0.0, 0.0],
        "routing adjacency loss": [0.9, 0.1, 0.0],
        "interface packet loss": [0.0, 1.0, 0.0],
        "power supply alarm": [0.0, 0.0, 1.0],
        "unrelated replacement": [-1.0, 0.0, 0.0],
        "routing failure": [1.0, 0.0, 0.0],
        "hardware alarm": [0.0, 0.0, 1.0],
    }

    def embed_documents(self, texts):
        return [self._vectors[text] for text in texts]

    def embed_queries(self, texts):
        return [self._vectors[text] for text in texts]


HAS_VECTOR_DEPS = True
try:
    import faiss  # noqa: F401
    import numpy  # noqa: F401
except ModuleNotFoundError:
    HAS_VECTOR_DEPS = False


requires_vector_deps = pytest.mark.skipif(
    not HAS_VECTOR_DEPS, reason="optional numpy/faiss dependencies are not installed"
)


@requires_vector_deps
def test_build_and_semantic_search_use_string_memory_ids():
    index = VectorMemoryIndex.build(
        {
            "mem-bgp": "bgp peer reset",
            "mem-interface": "interface packet loss",
            "mem-power": "power supply alarm",
        },
        DeterministicTestEmbedder(),
        base_index_type="hnsw",
    )

    hits = index.search("routing failure", k=2)

    assert [hit.memory_id for hit in hits] == ["mem-bgp", "mem-interface"]
    assert hits[0].version == 1
    assert index.health()["embedding_model"] == "test/explicit-vector-table"
    assert index.health()["base_index_type"] == "hnsw"


@requires_vector_deps
def test_upsert_version_and_delete_are_immediately_visible():
    index = VectorMemoryIndex.build(
        {"mem-bgp": "bgp peer reset", "mem-power": "power supply alarm"},
        DeterministicTestEmbedder(),
        applied_offset=10,
        base_index_type="hnsw",
        delta_ratio_threshold=1.0,
    )

    assert index.upsert("mem-new", "routing adjacency loss", offset=11, version=1)
    assert index.search("routing failure", 2)[0].memory_id == "mem-bgp"
    assert index.delete("mem-bgp", offset=12, version=2)
    assert index.search("routing failure", 2)[0].memory_id == "mem-new"
    assert index.delete("mem-bgp", offset=12, version=3) is False
    assert index.health()["applied_offset"] == 12
    assert index.health()["tombstones"] == 1


@requires_vector_deps
def test_updated_version_wins_and_compaction_reclaims_stale_vectors():
    index = VectorMemoryIndex.build(
        {
            "mem-bgp": "bgp peer reset",
            "mem-interface": "interface packet loss",
            "mem-power": "power supply alarm",
        },
        DeterministicTestEmbedder(),
        base_index_type="hnsw",
        obsolete_ratio_threshold=0.20,
        delta_ratio_threshold=1.0,
    )
    index.upsert("mem-bgp", "unrelated replacement", offset=1, version=2)
    index.delete("mem-interface", offset=2, version=2)

    assert index.should_compact()
    assert [hit.memory_id for hit in index.search("routing failure", 3)] == [
        "mem-power",
        "mem-bgp",
    ]
    generation = index.health()["generation"]

    assert index.compact() == generation + 1
    assert index.health()["delta"] == 0
    assert index.health()["obsolete"] == 0
    assert index.health()["tombstones"] == 0
    assert index.search("routing failure", 3)[-1].version == 2


def test_bge_adapter_delegates_to_real_encoder_with_query_asymmetry(monkeypatch):
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append((list(texts), kwargs))
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr("core.eval.dense_retrieval.embed", fake_embed)
    embedder = BGETextEmbedder(model_id="BAAI/bge-small-en-v1.5", dimension=3, batch_size=7)

    assert embedder.embed_documents(["document"])[0][0] == 1.0
    assert embedder.embed_queries(["query"])[0][0] == 1.0
    assert calls[0][1] == {
        "model_name": "BAAI/bge-small-en-v1.5",
        "is_query": False,
        "batch_size": 7,
    }
    assert calls[1][1]["is_query"] is True


def test_module_import_does_not_require_numpy_or_faiss():
    script = r'''
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"numpy", "faiss"}:
        raise ModuleNotFoundError(f"blocked {name}", name=name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from core.memory.vector_memory import BGETextEmbedder, VectorMemoryIndex
assert BGETextEmbedder().dimension == 384
assert VectorMemoryIndex is not None
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_vector_dependency_fails_only_when_index_is_built():
    script = r'''
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"numpy", "faiss"}:
        raise ModuleNotFoundError(f"blocked {name}", name=name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from core.memory.vector_memory import VectorMemoryDependencyError, VectorMemoryIndex
class E:
    dimension = 2
    model_id = "test"
    def embed_documents(self, texts): return [[1.0, 0.0] for _ in texts]
    def embed_queries(self, texts): return [[1.0, 0.0] for _ in texts]
try:
    VectorMemoryIndex.build({"memory-1": "text"}, E())
except VectorMemoryDependencyError:
    pass
else:
    raise AssertionError("build unexpectedly succeeded")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
