from __future__ import annotations

import pytest


pytest.importorskip("numpy")
pytest.importorskip("faiss")

from core.eval.vector_lifecycle_benchmark import run_vector_lifecycle_benchmark


@pytest.mark.performance
def test_small_vector_churn_benchmark_reclaims_and_restarts(tmp_path):
    result = run_vector_lifecycle_benchmark(
        size=1_000,
        dimension=32,
        updates=100,
        deletes=100,
        query_count=10,
        snapshot_dir=tmp_path,
    )
    assert result["before_compaction"]["compaction_due"]
    assert result["after_compaction"]["obsolete"] == 0
    assert result["physical_vectors_reclaimed"] == 200
    assert result["restart_results_equal"]
