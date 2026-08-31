# Benchmark result artifacts

These files are component microbenchmarks.  They are excluded from claims about
investigation accuracy, durable case continuity, memory benefit, remediation safety, or
whole-system readiness.

- `vector_index_100k.json`: 100,000 vectors, cold Flat/HNSW build and `efSearch` 32 to 256.
- `vector_index_1m.json`: 1,000,000 vectors, cold Flat/HNSW build and `efSearch` 32 to 1024.
- `index_lifecycle_100k.json`: 100,000 sparse documents followed by 10,000 updates and
  10,000 deletes; measures legacy rebuild-per-query cost, incremental query latency,
  physical reclamation, snapshot load, and exact BM25 equivalence.
- `vector_lifecycle_100k.json`: 100,000 vectors followed by the same 20% churn; measures
  pre/post-compaction latency, throughput, Recall@10, physical reclamation, and restart.
- `multiagent_parallel_fair.json`: deterministic I/O overlap for four handlers.  The
  handlers sleep and return fixed evidence; this artifact contains no model-quality or
  diagnosis-quality measurement.
- `public_aiops_business_20260831.json`: committed summary of the public replay over 733
  eligible RCAEval cases, 35 ITBench-Lite SRE snapshots and an AIOpsLab source audit.  It
  retains every ITBench scenario, worst RCA ranks and representative positive and negative
  memory transfers.  The evaluator command writes the complete case-level result.  Every
  business-value row has a separate `live_site_status`; public replay does not promote a
  deployed-system claim.

The large `.faiss` indexes are reproducible caches and are intentionally ignored. See
[`docs/HNSW_SCALE_BENCHMARK.md`](../docs/HNSW_SCALE_BENCHMARK.md) for methodology,
hardware, commands, and interpretation.

The dynamic-index design, sources, thresholds, and churn interpretation are documented in
[`docs/INDEX_LIFECYCLE_RESEARCH.md`](../docs/INDEX_LIFECYCLE_RESEARCH.md).
