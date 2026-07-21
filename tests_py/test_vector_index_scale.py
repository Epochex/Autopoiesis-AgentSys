"""Opt-in 100k/1m Flat versus HNSW regression tests.

Run explicitly with::

    RUN_VECTOR_SCALE_BENCH=1 pytest -m performance \
      tests_py/test_vector_index_scale.py -q -s

These checks use synthetic normalized vectors to isolate the FAISS index. They
do not measure embedding-model quality.
"""
from __future__ import annotations

import os

import pytest

from core.eval.vector_index_benchmark import BenchmarkConfig, run_size_benchmark


pytestmark = pytest.mark.performance


@pytest.mark.parametrize("size", [100_000, 1_000_000])
def test_hnsw_scale_keeps_recall_and_beats_flat_latency(size):
    if os.environ.get("RUN_VECTOR_SCALE_BENCH") != "1":
        pytest.skip("set RUN_VECTOR_SCALE_BENCH=1 to run the 100k/1m benchmark")
    pytest.importorskip("faiss")
    config = BenchmarkConfig(
        sizes=(size,),
        dim=128,
        queries=100,
        top_k=10,
        ef_search_values=(32, 64, 128, 256, 512, 1024),
        build_threads=min(8, os.cpu_count() or 1),
        latency_threads=1,
        throughput_threads=min(8, os.cpu_count() or 1),
        warmup_queries=10,
        throughput_repeats=3,
        index_cache_dir=os.environ.get("VECTOR_INDEX_CACHE_DIR"),
    )
    row = run_size_benchmark(size, config)
    sweep = row["hnsw"]["search_sweep"]
    assert sweep[-1]["recall_at_10_vs_flat"] >= 0.80
    qualified = [item for item in sweep if item["recall_at_10_vs_flat"] >= 0.80]
    fastest_qualified = min(qualified, key=lambda item: item["p95_ms"])
    assert fastest_qualified["p95_ms"] < row["flat"]["p95_ms"]
    assert fastest_qualified["batch_qps_median"] > row["flat"]["batch_qps_median"]
    assert all(
        right["recall_at_10_vs_flat"] >= left["recall_at_10_vs_flat"]
        for left, right in zip(sweep, sweep[1:])
    )
    assert row["hnsw"]["serialized_index_bytes"] < size * 128 * 4 * 2
