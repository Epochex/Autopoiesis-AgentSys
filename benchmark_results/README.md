# Benchmark result artifacts

This directory contains component microbenchmarks, public-dataset replays and controlled
host acceptance artifacts.  Each file states its analysis unit; measurements at different
levels remain separate.

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
  retains all RCAEval case results, all 542 chronological recurrence pairs and every ITBench
  scenario.  Every business-value row keeps `public_replay_status` and `live_site_status`
  separate.
- `business_value_acceptance_20260831.json`: six controlled host incidents from run
  `20260831T115350Z-d03425`.  It records one model-origin open root with two current signal
  families, five terminal action readbacks, one failed recovery stop, and a three-arm
  repeated-fault comparison with three repetitions per strategy.

The large `.faiss` indexes are reproducible caches and are intentionally ignored. See
[`docs/HNSW_SCALE_BENCHMARK.md`](../docs/HNSW_SCALE_BENCHMARK.md) for methodology,
hardware, commands, and interpretation.

The dynamic-index design, sources, thresholds, and churn interpretation are documented in
[`docs/INDEX_LIFECYCLE_RESEARCH.md`](../docs/INDEX_LIFECYCLE_RESEARCH.md).
