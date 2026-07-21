# Benchmark result artifacts

- `vector_index_100k.json`: 100,000 vectors, cold Flat/HNSW build and `efSearch` 32 to 256.
- `vector_index_1m.json`: 1,000,000 vectors, cold Flat/HNSW build and `efSearch` 32 to 1024.
- `index_lifecycle_100k.json`: 100,000 sparse documents followed by 10,000 updates and
  10,000 deletes; measures legacy rebuild-per-query cost, incremental query latency,
  physical reclamation, snapshot load, and exact BM25 equivalence.
- `vector_lifecycle_100k.json`: 100,000 vectors followed by the same 20% churn; measures
  pre/post-compaction latency, throughput, Recall@10, physical reclamation, and restart.

The large `.faiss` indexes are reproducible caches and are intentionally ignored. See
[`docs/HNSW_SCALE_BENCHMARK.md`](../docs/HNSW_SCALE_BENCHMARK.md) for methodology,
hardware, commands, and interpretation.

The dynamic-index design, sources, thresholds, and churn interpretation are documented in
[`docs/INDEX_LIFECYCLE_RESEARCH.md`](../docs/INDEX_LIFECYCLE_RESEARCH.md).
