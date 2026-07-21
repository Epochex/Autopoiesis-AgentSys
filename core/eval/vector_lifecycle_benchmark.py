"""Scale benchmark for FAISS base/delta churn and physical reclamation."""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from core.eval.vector_index_benchmark import generate_queries, generate_vectors
from core.memory.vector_lifecycle import VectorIndexLifecycle


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[int(round((len(ordered) - 1) * 0.95))] if ordered else 0.0


def _search(index: VectorIndexLifecycle, queries: Any, k: int) -> tuple[list[list[str]], dict[str, float]]:
    timings: list[float] = []
    rows: list[list[str]] = []
    started = time.perf_counter()
    for query in queries:
        one = time.perf_counter()
        rows.append(index.search(query, k))
        timings.append((time.perf_counter() - one) * 1_000)
    elapsed = time.perf_counter() - started
    return rows, {
        "p50_ms": round(statistics.median(timings), 4),
        "p95_ms": round(_p95(timings), 4),
        "qps": round(len(queries) / elapsed, 2),
    }


def _recall(actual: list[list[str]], exact: list[list[str]], k: int) -> float:
    return sum(len(set(left[:k]) & set(right[:k])) / k for left, right in zip(actual, exact)) / len(exact)


def run_vector_lifecycle_benchmark(
    *,
    size: int = 100_000,
    dimension: int = 128,
    updates: int = 10_000,
    deletes: int = 10_000,
    query_count: int = 50,
    k: int = 10,
    seed: int = 31,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    import numpy as np

    if min(size, dimension, query_count, k) <= 0 or updates + deletes > size:
        raise ValueError("invalid benchmark configuration")
    doc_ids = [f"doc-{number:08d}" for number in range(size)]
    vectors = generate_vectors(size, dimension, seed)

    started = time.perf_counter()
    index = VectorIndexLifecycle.build(
        doc_ids,
        vectors,
        base_index_type="hnsw",
        delta_max_entries=max(1, updates),
        delta_ratio_threshold=0.10,
        obsolete_ratio_threshold=0.20,
    )
    initial_build_seconds = time.perf_counter() - started

    replacements = generate_vectors(updates, dimension, seed + 1)
    offset = 0
    for number in range(updates):
        offset += 1
        index.upsert(doc_ids[number], replacements[number], offset=offset)
        vectors[number] = replacements[number]
    for number in range(updates, updates + deletes):
        offset += 1
        index.delete(doc_ids[number], offset=offset)

    live_ids = doc_ids[:updates] + doc_ids[updates + deletes :]
    live_vectors = np.ascontiguousarray(
        np.concatenate((vectors[:updates], vectors[updates + deletes :]), axis=0),
        dtype="float32",
    )
    queries, _ = generate_queries(live_vectors, query_count, seed + 2, 0.01)
    exact_index = VectorIndexLifecycle.build(live_ids, live_vectors, base_index_type="flat")
    exact, _ = _search(exact_index, queries, k)

    before_health = index.health()
    before_rows, before_query = _search(index, queries, k)
    compact_started = time.perf_counter()
    index.compact()
    compaction_seconds = time.perf_counter() - compact_started
    after_health = index.health()
    after_rows, after_query = _search(index, queries, k)

    root = Path(snapshot_dir) if snapshot_dir else Path(tempfile.mkdtemp(prefix="vector-lifecycle-"))
    snapshot = index.save(root)
    snapshot_bytes = sum(path.stat().st_size for path in snapshot.iterdir() if path.is_file())
    load_started = time.perf_counter()
    restored = VectorIndexLifecycle.load(root)
    load_seconds = time.perf_counter() - load_started
    restored_rows, _ = _search(restored, queries, k)

    return {
        "config": {
            "documents": size,
            "dimension": dimension,
            "updates": updates,
            "deletes": deletes,
            "queries": query_count,
            "k": k,
            "seed": seed,
        },
        "initial_build_seconds": round(initial_build_seconds, 4),
        "before_compaction": before_health,
        "after_compaction": after_health,
        "before_query": before_query,
        "after_query": after_query,
        "recall_at_k_before": round(_recall(before_rows, exact, k), 4),
        "recall_at_k_after": round(_recall(after_rows, exact, k), 4),
        "compaction_seconds": round(compaction_seconds, 4),
        "physical_vectors_reclaimed": (
            int(before_health["physical_vectors"]) - int(after_health["physical_vectors"])
        ),
        "snapshot_bytes": snapshot_bytes,
        "load_seconds": round(load_seconds, 4),
        "restart_results_equal": restored_rows == after_rows,
        "snapshot_directory": str(root),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=100_000)
    parser.add_argument("--dimension", type=int, default=128)
    parser.add_argument("--updates", type=int, default=10_000)
    parser.add_argument("--deletes", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_vector_lifecycle_benchmark(
        size=args.size,
        dimension=args.dimension,
        updates=args.updates,
        deletes=args.deletes,
        query_count=args.queries,
        snapshot_dir=args.snapshot_dir,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
