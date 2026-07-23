from core.eval.index_lifecycle_benchmark import run_sparse_lifecycle_benchmark


def test_small_churn_benchmark_reclaims_space_and_preserves_rankings(tmp_path):
    result = run_sparse_lifecycle_benchmark(
        size=200,
        updates=40,
        deletes=40,
        query_count=12,
        snapshot_dir=tmp_path,
    )
    assert result["ranking_equal_to_monolithic_bm25"]
    assert result["post_compaction_ranking_equal_to_monolithic_bm25"]
    assert result["restart_ranking_equal"]
    assert result["schema_version"] == 2
    assert result["benchmark"] == "segmented-vs-resident-monolithic-bm25"
    assert result["resident_query"]["segmented_before_compaction"]["p95_ms"] > 0
    assert result["resident_query"]["monolithic_full_scan"]["p95_ms"] > 0
    assert "legacy_rebuild_per_query" not in result
    assert result["maintenance"]["incremental_mutation_seconds"] > 0
    assert result["maintenance"]["fresh_monolithic_rebuild_seconds"] > 0
    assert result["compacted"]
    assert result["before_compaction"]["obsolete_ratio"] >= 0.20
    assert result["after_compaction"]["obsolete_entries"] == 0
    # 40 replaced base versions plus 40 deleted base versions and 40 delete
    # markers are reclaimed; audit history remains in the source event log.
    assert result["physical_entries_reclaimed"] == 120
    assert result["snapshot_bytes_after"] < result["snapshot_bytes_before"]
