from core.eval.multiagent_parallel_benchmark import (
    ParallelBenchmarkConfig,
    run_benchmark,
)


def test_same_work_parallel_benchmark_preserves_evidence_and_improves_balanced_latency():
    report = run_benchmark(
        ParallelBenchmarkConfig(worker_values=(1, 2, 4), repeats=3, warmups=0),
        scenarios={"tiny_balanced": (10.0, 10.0, 10.0, 10.0)},
    )
    rows = report["results"][0]["rows"]
    assert [row["workers"] for row in rows] == [1, 2, 4]
    assert {row["evidence_count"] for row in rows} == {4}
    assert {row["tool_cost"] for row in rows} == {1.0}
    assert rows[-1]["observed_peak_overlap"] == 4
    assert rows[-1]["p95_ms"] < rows[0]["p95_ms"]
    assert rows[-1]["p95_speedup_vs_serial"] > 2.0
