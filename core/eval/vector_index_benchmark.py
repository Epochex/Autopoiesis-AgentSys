"""Reproducible Flat versus HNSW scale benchmark.

This benchmark isolates the approximate-nearest-neighbour index from embedding
model quality. It generates one deterministic normalized float32 corpus, uses
FAISS ``IndexFlatIP`` as the exact top-k oracle, then evaluates the production
``IndexHNSWFlat`` configuration over the same vectors and queries.

The default scale run covers 100,000 and 1,000,000 vectors. Large runs are a
separate command and pytest marker; importing this module or running the normal
test suite never allocates the large corpus.

Example::

    python -m core.eval.vector_index_benchmark \
      --sizes 100000 1000000 --dim 128 --queries 100 \
      --output benchmark_results/vector_index_100k_1m.json
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import os
import platform
import resource
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from core.eval.dense_retrieval import (
    DEFAULT_HNSW_EF_CONSTRUCTION,
    DEFAULT_HNSW_EF_SEARCH,
    DEFAULT_HNSW_M,
    DenseIndex,
)


@dataclass(frozen=True)
class BenchmarkConfig:
    sizes: tuple[int, ...] = (100_000, 1_000_000)
    dim: int = 128
    queries: int = 100
    top_k: int = 10
    seed: int = 20260721
    query_noise: float = 0.02
    query_mode: str = "perturbed_in_corpus"
    hnsw_m: int = DEFAULT_HNSW_M
    ef_construction: int = DEFAULT_HNSW_EF_CONSTRUCTION
    ef_search_values: tuple[int, ...] = (32, 64, DEFAULT_HNSW_EF_SEARCH, 256, 512, 1024)
    build_threads: int = 8
    latency_threads: int = 1
    throughput_threads: int = 8
    warmup_queries: int = 10
    throughput_repeats: int = 3
    recall_targets: tuple[float, ...] = (0.95, 0.99)
    index_cache_dir: str | None = None

    def validate(self) -> None:
        positive = {
            "dim": self.dim,
            "queries": self.queries,
            "top_k": self.top_k,
            "hnsw_m": self.hnsw_m,
            "ef_construction": self.ef_construction,
            "build_threads": self.build_threads,
            "latency_threads": self.latency_threads,
            "throughput_threads": self.throughput_threads,
            "throughput_repeats": self.throughput_repeats,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.sizes or any(size <= 0 for size in self.sizes):
            raise ValueError("sizes must contain positive values")
        if not self.ef_search_values or any(value <= 0 for value in self.ef_search_values):
            raise ValueError("ef_search_values must contain positive values")
        if self.queries > min(self.sizes):
            raise ValueError("queries cannot exceed the smallest corpus")
        if self.top_k > min(self.sizes):
            raise ValueError("top_k cannot exceed the smallest corpus")
        if self.query_noise < 0:
            raise ValueError("query_noise must be non-negative")
        if self.query_mode not in {"perturbed_in_corpus", "independent"}:
            raise ValueError("query_mode must be perturbed_in_corpus or independent")
        if self.warmup_queries < 0:
            raise ValueError("warmup_queries must be non-negative")
        if any(not 0.0 < target <= 1.0 for target in self.recall_targets):
            raise ValueError("recall_targets must be in (0, 1]")


def generate_vectors(count: int, dim: int, seed: int):
    """Return deterministic L2-normalized float32 vectors."""
    import numpy as np

    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((count, dim), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= np.maximum(norms, np.float32(1e-12))
    return np.ascontiguousarray(vectors)


def generate_queries(vectors, count: int, seed: int, noise: float):
    """Sample corpus vectors and perturb them into deterministic queries."""
    import numpy as np

    rng = np.random.default_rng(seed)
    source_ids = rng.choice(len(vectors), size=count, replace=False)
    queries = vectors[source_ids].copy()
    if noise:
        queries += rng.normal(0.0, noise, size=queries.shape).astype("float32")
    norms = np.linalg.norm(queries, axis=1, keepdims=True)
    queries /= np.maximum(norms, np.float32(1e-12))
    return np.ascontiguousarray(queries), source_ids.astype("int64")


def generate_independent_queries(count: int, dim: int, seed: int):
    """Generate held-out normalized queries that are not copied from the corpus."""
    import numpy as np

    rng = np.random.default_rng(seed)
    queries = rng.standard_normal((count, dim), dtype=np.float32)
    norms = np.linalg.norm(queries, axis=1, keepdims=True)
    queries /= np.maximum(norms, np.float32(1e-12))
    return np.ascontiguousarray(queries)


def recall_at_k(actual, expected, k: int) -> float:
    """Macro recall against exact top-k neighbours."""
    if k <= 0:
        raise ValueError("k must be positive")
    if len(actual) != len(expected):
        raise ValueError("actual and expected row counts differ")
    if not len(actual):
        return 0.0
    total = 0.0
    for actual_row, expected_row in zip(actual, expected):
        truth = set(int(item) for item in expected_row[:k] if int(item) >= 0)
        got = set(int(item) for item in actual_row[:k] if int(item) >= 0)
        total += len(got & truth) / max(1, len(truth))
    return total / len(actual)


def latency_summary(milliseconds: Sequence[float]) -> dict[str, float]:
    """Return stable per-query latency percentiles without scipy."""
    import numpy as np

    if not milliseconds:
        raise ValueError("milliseconds must not be empty")
    values = np.asarray(milliseconds, dtype="float64")
    return {
        "p50_ms": round(float(np.percentile(values, 50)), 4),
        "p95_ms": round(float(np.percentile(values, 95)), 4),
        "p99_ms": round(float(np.percentile(values, 99)), 4),
        "mean_ms": round(float(values.mean()), 4),
    }


def _rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    # Linux reports KiB, macOS bytes. This repository deploys on Linux; keep a
    # portable fallback for local development.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _trim_heap() -> None:
    """Return freed FAISS allocations to the OS when glibc exposes malloc_trim."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _measure_latency(index, queries, k: int, threads: int, warmup: int):
    import faiss
    import numpy as np

    faiss.omp_set_num_threads(threads)
    for query in queries[: min(warmup, len(queries))]:
        index.search(np.ascontiguousarray(query[None, :]), k)
    timings: list[float] = []
    rows: list[Any] = []
    started = time.perf_counter()
    for query in queries:
        query_started = time.perf_counter()
        _distances, indices = index.search(np.ascontiguousarray(query[None, :]), k)
        timings.append((time.perf_counter() - query_started) * 1000.0)
        rows.append(indices[0].copy())
    elapsed = time.perf_counter() - started
    return np.asarray(rows, dtype="int64"), {
        **latency_summary(timings),
        "sequential_qps": round(len(queries) / elapsed, 2),
        "latency_threads": threads,
    }


def _measure_throughput(index, queries, k: int, threads: int, repeats: int) -> dict[str, float | int]:
    import faiss

    faiss.omp_set_num_threads(threads)
    rates: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        index.search(queries, k)
        elapsed = time.perf_counter() - started
        rates.append(len(queries) / elapsed)
    return {
        "batch_qps_median": round(statistics.median(rates), 2),
        "batch_qps_min": round(min(rates), 2),
        "throughput_threads": threads,
        "repeats": repeats,
    }


def _hnsw_cache_path(config: BenchmarkConfig, size: int) -> Path | None:
    if config.index_cache_dir is None:
        return None
    name = (
        f"hnsw_n{size}_d{config.dim}_seed{config.seed + size}"
        f"_m{config.hnsw_m}_efc{config.ef_construction}.faiss"
    )
    return Path(config.index_cache_dir) / name


def _build_index(vectors, index_type: str, config: BenchmarkConfig, size: int) -> tuple[Any, dict[str, Any]]:
    import faiss

    faiss.omp_set_num_threads(config.build_threads)
    rss_before = _rss_bytes()
    cache_path = _hnsw_cache_path(config, size) if index_type == "hnsw" else None
    cache_hit = bool(cache_path and cache_path.exists())
    build_seconds = 0.0
    load_seconds = 0.0
    cache_write_seconds = 0.0
    if cache_hit:
        started = time.perf_counter()
        index = faiss.read_index(str(cache_path))
        load_seconds = time.perf_counter() - started
        if index.ntotal != len(vectors) or index.d != config.dim:
            raise ValueError(f"cached index shape mismatch: {cache_path}")
    else:
        started = time.perf_counter()
        wrapper = DenseIndex(
            list(range(len(vectors))),
            vectors,
            index_type,
            hnsw_m=config.hnsw_m,
            hnsw_ef_construction=config.ef_construction,
            hnsw_ef_search=config.ef_search_values[0],
        )
        build_seconds = time.perf_counter() - started
        index = wrapper.index
        del wrapper
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            write_started = time.perf_counter()
            faiss.write_index(index, str(temporary))
            temporary.replace(cache_path)
            cache_write_seconds = time.perf_counter() - write_started
    rss_after = _rss_bytes()
    if cache_path is not None and cache_path.exists():
        serialized_bytes = cache_path.stat().st_size
    else:
        serialized_bytes = len(faiss.serialize_index(index))
    return index, {
        "build_seconds": round(build_seconds, 4),
        "build_vectors_per_second": round(len(vectors) / build_seconds, 2) if build_seconds else None,
        "load_seconds": round(load_seconds, 4),
        "cache_write_seconds": round(cache_write_seconds, 4),
        "cache_hit": cache_hit,
        "cache_path": str(cache_path) if cache_path is not None else None,
        "serialized_index_bytes": serialized_bytes,
        "rss_before_build_bytes": rss_before,
        "rss_after_build_bytes": rss_after,
        "rss_build_delta_bytes": max(0, rss_after - rss_before),
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "build_threads": config.build_threads,
    }


def _fingerprint(config: BenchmarkConfig, size: int, source_ids) -> str:
    payload = {
        "size": size,
        "dim": config.dim,
        "seed": config.seed,
        "query_noise": config.query_noise,
        "query_mode": config.query_mode,
        "source_ids": [int(item) for item in source_ids],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def matched_recall_summary(
    flat_result: dict[str, Any],
    hnsw_rows: Sequence[dict[str, Any]],
    recall_targets: Sequence[float],
) -> list[dict[str, Any]]:
    """Select the lowest-latency measured HNSW row that reaches each recall target."""
    summary: list[dict[str, Any]] = []
    for target in recall_targets:
        eligible = [row for row in hnsw_rows if row["recall_at_k_vs_flat"] >= target]
        if not eligible:
            summary.append({"target_recall_at_k": target, "reached": False})
            continue
        best = min(eligible, key=lambda row: (row["p95_ms"], row["ef_search"]))
        summary.append({
            "target_recall_at_k": target,
            "reached": True,
            "ef_search": best["ef_search"],
            "observed_recall_at_k": best["recall_at_k_vs_flat"],
            "hnsw_p95_ms": best["p95_ms"],
            "flat_p95_ms": flat_result["p95_ms"],
            "p95_speedup_vs_flat": round(flat_result["p95_ms"] / best["p95_ms"], 4),
            "hnsw_batch_qps": best["batch_qps_median"],
            "flat_batch_qps": flat_result["batch_qps_median"],
            "batch_qps_speedup_vs_flat": round(
                best["batch_qps_median"] / flat_result["batch_qps_median"], 4
            ),
        })
    return summary


def run_size_benchmark(size: int, config: BenchmarkConfig) -> dict[str, Any]:
    """Benchmark one corpus size and return a JSON-serializable result."""
    import faiss

    config.validate()
    generated_at = time.perf_counter()
    vectors = generate_vectors(size, config.dim, config.seed + size)
    if config.query_mode == "independent":
        queries = generate_independent_queries(
            config.queries,
            config.dim,
            config.seed + size + 1,
        )
        source_ids = [-1] * config.queries
        dataset = "deterministic normalized Gaussian corpus with independent held-out queries"
    else:
        queries, source_ids = generate_queries(
            vectors,
            config.queries,
            config.seed + size + 1,
            config.query_noise,
        )
        dataset = "deterministic normalized Gaussian vectors with perturbed in-corpus queries"
    generation_seconds = time.perf_counter() - generated_at
    corpus_rss = _rss_bytes()

    flat, flat_build = _build_index(vectors, "flat", config, size)
    exact, flat_latency = _measure_latency(
        flat,
        queries,
        config.top_k,
        config.latency_threads,
        config.warmup_queries,
    )
    flat_throughput = _measure_throughput(
        flat,
        queries,
        config.top_k,
        config.throughput_threads,
        config.throughput_repeats,
    )
    flat_result = {
        "index": "IndexFlatIP",
        "recall_at_10_vs_flat": 1.0 if config.top_k == 10 else None,
        "recall_at_k_vs_flat": 1.0,
        **flat_build,
        **flat_latency,
        **flat_throughput,
    }
    del flat
    _trim_heap()

    hnsw, hnsw_build = _build_index(vectors, "hnsw", config, size)
    hnsw_rows: list[dict[str, Any]] = []
    for ef_search in config.ef_search_values:
        hnsw.hnsw.efSearch = ef_search
        actual, latency = _measure_latency(
            hnsw,
            queries,
            config.top_k,
            config.latency_threads,
            config.warmup_queries,
        )
        throughput = _measure_throughput(
            hnsw,
            queries,
            config.top_k,
            config.throughput_threads,
            config.throughput_repeats,
        )
        recall = recall_at_k(actual, exact, config.top_k)
        hnsw_rows.append({
            "ef_search": ef_search,
            "recall_at_1_vs_flat": round(recall_at_k(actual, exact, 1), 6),
            "recall_at_10_vs_flat": round(recall, 6) if config.top_k == 10 else None,
            "recall_at_k_vs_flat": round(recall, 6),
            **latency,
            **throughput,
        })

    faiss_version = getattr(faiss, "__version__", "unknown")
    result = {
        "size": size,
        "dim": config.dim,
        "queries": config.queries,
        "top_k": config.top_k,
        "dataset": dataset,
        "dataset_fingerprint": _fingerprint(config, size, source_ids),
        "query_noise": config.query_noise,
        "generation_seconds": round(generation_seconds, 4),
        "raw_vector_bytes": int(vectors.nbytes),
        "rss_after_corpus_bytes": corpus_rss,
        "flat": flat_result,
        "hnsw": {
            "index": "IndexHNSWFlat",
            "m": config.hnsw_m,
            "ef_construction": config.ef_construction,
            **hnsw_build,
            "search_sweep": hnsw_rows,
            "matched_recall": matched_recall_summary(
                flat_result,
                hnsw_rows,
                config.recall_targets,
            ),
        },
        "faiss_version": faiss_version,
    }
    del hnsw, exact, queries, vectors
    _trim_heap()
    return result


def environment_metadata() -> dict[str, Any]:
    import faiss
    import numpy as np

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "faiss": getattr(faiss, "__version__", "unknown"),
        "numpy": np.__version__,
    }


def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    config.validate()
    started = time.perf_counter()
    rows = [run_size_benchmark(size, config) for size in config.sizes]
    return {
        "schema_version": 2,
        "benchmark": "flat-vs-hnsw-scale",
        "config": asdict(config),
        "environment": environment_metadata(),
        "results": rows,
        "total_seconds": round(time.perf_counter() - started, 4),
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--query-noise", type=float, default=0.02)
    parser.add_argument(
        "--query-mode",
        choices=["perturbed_in_corpus", "independent"],
        default="perturbed_in_corpus",
    )
    parser.add_argument("--hnsw-m", type=int, default=DEFAULT_HNSW_M)
    parser.add_argument("--ef-construction", type=int, default=DEFAULT_HNSW_EF_CONSTRUCTION)
    parser.add_argument("--ef-search", type=int, nargs="+", default=[32, 64, 128, 256, 512, 1024])
    parser.add_argument("--build-threads", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--latency-threads", type=int, default=1)
    parser.add_argument("--throughput-threads", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--throughput-repeats", type=int, default=3)
    parser.add_argument("--warmup-queries", type=int, default=10)
    parser.add_argument("--recall-targets", type=float, nargs="+", default=[0.95, 0.99])
    parser.add_argument("--index-cache-dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    config = BenchmarkConfig(
        sizes=tuple(args.sizes),
        dim=args.dim,
        queries=args.queries,
        top_k=args.top_k,
        seed=args.seed,
        query_noise=args.query_noise,
        query_mode=args.query_mode,
        hnsw_m=args.hnsw_m,
        ef_construction=args.ef_construction,
        ef_search_values=tuple(args.ef_search),
        build_threads=args.build_threads,
        latency_threads=args.latency_threads,
        throughput_threads=args.throughput_threads,
        throughput_repeats=args.throughput_repeats,
        warmup_queries=args.warmup_queries,
        recall_targets=tuple(args.recall_targets),
        index_cache_dir=str(args.index_cache_dir) if args.index_cache_dir else None,
    )
    report = run_benchmark(config)
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        write_report(report, args.output)
        print(f"wrote {args.output}", file=sys.stderr)
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
