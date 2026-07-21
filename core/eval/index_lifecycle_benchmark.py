"""Reproducible churn benchmark for the incremental retrieval lifecycle."""
from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from core.memory.bm25 import BM25Index
from core.memory.segmented_bm25 import SegmentedBM25Index


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[position]


def _documents(size: int, *, seed: int) -> dict[str, list[str]]:
    rng = random.Random(seed)
    documents: dict[str, list[str]] = {}
    for number in range(size):
        topic = number % 2_000
        tokens = [
            f"topic{topic}",
            f"asset{number % 10_000}",
            f"region{number % 32}",
            f"state{number % 17}",
        ]
        tokens.extend(f"term{rng.randrange(20_000)}" for _ in range(8))
        documents[f"doc-{number:08d}"] = tokens
    return documents


def _queries(count: int, *, seed: int) -> list[list[str]]:
    rng = random.Random(seed)
    return [
        [f"topic{rng.randrange(2_000)}", f"region{rng.randrange(32)}", f"term{rng.randrange(20_000)}"]
        for _ in range(count)
    ]


def run_sparse_lifecycle_benchmark(
    *,
    size: int = 100_000,
    updates: int = 10_000,
    deletes: int = 10_000,
    query_count: int = 100,
    seed: int = 23,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    if min(size, query_count) <= 0 or min(updates, deletes) < 0 or updates + deletes > size:
        raise ValueError("invalid benchmark size or churn")
    documents = _documents(size, seed=seed)
    queries = _queries(query_count, seed=seed + 1)
    index = SegmentedBM25Index(
        seal_threshold=1_000,
        compact_segment_threshold=256,
        obsolete_ratio_threshold=0.20,
        min_compaction_entries=min(1_000, size),
    )

    started = time.perf_counter()
    for offset, (doc_id, tokens) in enumerate(documents.items(), start=1):
        index.upsert(doc_id, tokens, offset)
    initial_ingest_seconds = time.perf_counter() - started
    index.compact(force=True)

    offset = size
    for number in range(updates):
        doc_id = f"doc-{number:08d}"
        offset += 1
        replacement = ["updated", f"topic{number % 2_000}", f"revision{offset}"]
        documents[doc_id] = replacement
        index.upsert(doc_id, replacement, offset)
    for number in range(updates, updates + deletes):
        doc_id = f"doc-{number:08d}"
        offset += 1
        documents.pop(doc_id)
        index.delete(doc_id, offset)

    before = index.health()
    latencies_ms: list[float] = []
    observed: list[list[tuple[str, float]]] = []
    for query in queries:
        query_started = time.perf_counter()
        observed.append(index.rank_with_scores(query, 10))
        latencies_ms.append((time.perf_counter() - query_started) * 1_000)

    exact_started = time.perf_counter()
    exact = BM25Index(documents)
    exact_build_seconds = time.perf_counter() - exact_started
    equivalent = all(
        actual == exact.rank_with_scores("", 10, query_tokens=query)
        for actual, query in zip(observed, queries)
    )
    legacy_latencies_ms: list[float] = []
    for query in queries[: min(5, len(queries))]:
        legacy_started = time.perf_counter()
        BM25Index(documents).rank_with_scores("", 10, query_tokens=query)
        legacy_latencies_ms.append((time.perf_counter() - legacy_started) * 1_000)

    root = Path(snapshot_dir) if snapshot_dir else Path(tempfile.mkdtemp(prefix="index-lifecycle-"))
    root.mkdir(parents=True, exist_ok=True)
    before_path = root / "before-compaction.json"
    after_path = root / "after-compaction.json"
    index.save(before_path)

    compact_started = time.perf_counter()
    compacted = index.maybe_compact()
    compaction_seconds = time.perf_counter() - compact_started
    after = index.health()
    index.save(after_path)
    restored_started = time.perf_counter()
    restored = SegmentedBM25Index.load(after_path)
    restore_seconds = time.perf_counter() - restored_started
    restart_equivalent = all(
        restored.rank_with_scores(query, 10) == exact.rank_with_scores("", 10, query_tokens=query)
        for query in queries
    )

    physical_before = int(before["physical_entries"])
    physical_after = int(after["physical_entries"])
    return {
        "config": {
            "documents": size,
            "updates": updates,
            "deletes": deletes,
            "queries": query_count,
            "seed": seed,
        },
        "initial_ingest_seconds": round(initial_ingest_seconds, 4),
        "fresh_monolithic_build_seconds": round(exact_build_seconds, 4),
        "query": {
            "p50_ms": round(statistics.median(latencies_ms), 4),
            "p95_ms": round(_percentile(latencies_ms, 0.95), 4),
            "p99_ms": round(_percentile(latencies_ms, 0.99), 4),
            "qps": round(query_count / (sum(latencies_ms) / 1_000), 2),
        },
        "legacy_rebuild_per_query": {
            "samples": len(legacy_latencies_ms),
            "p50_ms": round(statistics.median(legacy_latencies_ms), 4),
            "p95_ms": round(_percentile(legacy_latencies_ms, 0.95), 4),
            "p95_speedup_x": round(
                _percentile(legacy_latencies_ms, 0.95) / _percentile(latencies_ms, 0.95),
                2,
            ),
        },
        "before_compaction": before,
        "after_compaction": after,
        "compacted": compacted,
        "compaction_seconds": round(compaction_seconds, 4),
        "physical_entries_reclaimed": physical_before - physical_after,
        "physical_reduction_percent": round(
            100 * (physical_before - physical_after) / physical_before if physical_before else 0.0,
            2,
        ),
        "snapshot_bytes_before": before_path.stat().st_size,
        "snapshot_bytes_after": after_path.stat().st_size,
        "restore_seconds": round(restore_seconds, 4),
        "ranking_equal_to_monolithic_bm25": equivalent,
        "restart_ranking_equal": restart_equivalent,
        "snapshot_directory": str(root),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=100_000)
    parser.add_argument("--updates", type=int, default=10_000)
    parser.add_argument("--deletes", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_sparse_lifecycle_benchmark(
        size=args.size,
        updates=args.updates,
        deletes=args.deletes,
        query_count=args.queries,
        seed=args.seed,
        snapshot_dir=args.snapshot_dir,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
