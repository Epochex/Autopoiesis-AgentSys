"""Fast contract tests for the Flat versus HNSW scale benchmark."""
from __future__ import annotations

import json

import pytest

from core.eval.vector_index_benchmark import (
    BenchmarkConfig,
    generate_queries,
    generate_vectors,
    latency_summary,
    recall_at_k,
    run_benchmark,
    write_report,
)


def test_config_rejects_invalid_scale_parameters():
    with pytest.raises(ValueError, match="sizes"):
        BenchmarkConfig(sizes=(0,)).validate()
    with pytest.raises(ValueError, match="queries"):
        BenchmarkConfig(sizes=(10,), queries=11).validate()
    with pytest.raises(ValueError, match="ef_search_values"):
        BenchmarkConfig(sizes=(10,), queries=2, ef_search_values=()).validate()


def test_vector_and_query_generation_is_deterministic_and_normalized():
    np = pytest.importorskip("numpy")
    first = generate_vectors(128, 24, 7)
    second = generate_vectors(128, 24, 7)
    assert first.dtype == np.float32
    assert first.flags.c_contiguous
    assert np.array_equal(first, second)
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-5)

    q1, ids1 = generate_queries(first, 12, 8, 0.02)
    q2, ids2 = generate_queries(first, 12, 8, 0.02)
    assert np.array_equal(ids1, ids2)
    assert np.array_equal(q1, q2)
    assert np.allclose(np.linalg.norm(q1, axis=1), 1.0, atol=1e-5)


def test_recall_at_k_uses_flat_top_k_as_the_oracle():
    np = pytest.importorskip("numpy")
    exact = np.array([[1, 2, 3], [4, 5, 6]])
    same = np.array([[1, 2, 3], [4, 5, 6]])
    half = np.array([[1, 8, 9], [4, 8, 9]])
    assert recall_at_k(same, exact, 3) == 1.0
    assert recall_at_k(half, exact, 3) == pytest.approx(1 / 3)
    with pytest.raises(ValueError, match="row counts"):
        recall_at_k(half[:1], exact, 3)


def test_latency_summary_reports_all_required_percentiles():
    row = latency_summary([1.0, 2.0, 3.0, 4.0, 100.0])
    assert set(row) == {"p50_ms", "p95_ms", "p99_ms", "mean_ms"}
    assert row["p50_ms"] == 3.0
    assert row["p95_ms"] >= row["p50_ms"]
    assert row["p99_ms"] >= row["p95_ms"]


def test_dense_index_exposes_configurable_hnsw_parameters():
    np = pytest.importorskip("numpy")
    pytest.importorskip("faiss")
    from core.eval.dense_retrieval import DenseIndex

    vectors = generate_vectors(256, 32, 9)
    index = DenseIndex(
        list(range(len(vectors))),
        vectors,
        "hnsw",
        hnsw_m=12,
        hnsw_ef_construction=77,
        hnsw_ef_search=33,
    )
    assert index.index.hnsw.nb_neighbors(0) == 24
    assert index.index.hnsw.efConstruction == 77
    assert index.index.hnsw.efSearch == 33
    distances, ids = index.index.search(np.ascontiguousarray(vectors[:4]), 10)
    assert distances.shape == ids.shape == (4, 10)


def test_small_end_to_end_benchmark_has_metrics_and_recall_tradeoff():
    pytest.importorskip("faiss")
    config = BenchmarkConfig(
        sizes=(2_000,),
        dim=32,
        queries=20,
        top_k=10,
        ef_search_values=(8, 32),
        build_threads=2,
        latency_threads=1,
        throughput_threads=2,
        warmup_queries=2,
        throughput_repeats=1,
    )
    report = run_benchmark(config)
    assert report["schema_version"] == 1
    assert report["benchmark"] == "flat-vs-hnsw-scale"
    row = report["results"][0]
    assert row["size"] == 2_000
    assert row["raw_vector_bytes"] == 2_000 * 32 * 4
    assert row["flat"]["recall_at_k_vs_flat"] == 1.0
    assert row["flat"]["serialized_index_bytes"] > 0
    assert row["hnsw"]["serialized_index_bytes"] > 0
    sweep = row["hnsw"]["search_sweep"]
    assert [item["ef_search"] for item in sweep] == [8, 32]
    assert 0.0 <= sweep[0]["recall_at_k_vs_flat"] <= 1.0
    assert 0.0 <= sweep[0]["recall_at_1_vs_flat"] <= 1.0
    assert sweep[1]["recall_at_k_vs_flat"] >= sweep[0]["recall_at_k_vs_flat"]
    for method in [row["flat"], *sweep]:
        assert method["p95_ms"] > 0
        assert method["sequential_qps"] > 0
        assert method["batch_qps_median"] > 0


def test_report_write_is_complete_and_atomic(tmp_path):
    output = tmp_path / "nested" / "report.json"
    payload = {"schema_version": 1, "results": [{"size": 10}]}
    write_report(payload, output)
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert not output.with_suffix(".json.tmp").exists()


def test_hnsw_index_cache_avoids_rebuilding(tmp_path):
    pytest.importorskip("faiss")
    config = BenchmarkConfig(
        sizes=(1_000,),
        dim=24,
        queries=10,
        ef_search_values=(16,),
        build_threads=2,
        throughput_threads=2,
        throughput_repeats=1,
        index_cache_dir=str(tmp_path),
    )
    first = run_benchmark(config)["results"][0]
    second = run_benchmark(config)["results"][0]
    assert first["hnsw"]["cache_hit"] is False
    assert first["hnsw"]["build_seconds"] > 0
    assert second["hnsw"]["cache_hit"] is True
    assert second["hnsw"]["build_seconds"] == 0
    assert second["hnsw"]["load_seconds"] > 0
    assert first["hnsw"]["serialized_index_bytes"] == second["hnsw"]["serialized_index_bytes"]
    assert [item["recall_at_k_vs_flat"] for item in first["hnsw"]["search_sweep"]] == [
        item["recall_at_k_vs_flat"] for item in second["hnsw"]["search_sweep"]
    ]
