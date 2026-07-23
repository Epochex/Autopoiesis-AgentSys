"""Controlled benchmark for bounded parallel specialist execution.

The benchmark runs the exact same four role assignments with one, two and four
worker threads.  Handlers emulate independent I/O-bound evidence calls with
deterministic delays; no model-quality claim is made.  Evidence and cost must be
identical at every worker count, so speedup cannot be bought by doing less work.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from core.orchestrator.agents import ParallelExecutorAgent, RoleAssignment, RoleFinding
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


@dataclass(frozen=True)
class ParallelBenchmarkConfig:
    worker_values: tuple[int, ...] = (1, 2, 4)
    repeats: int = 30
    warmups: int = 3

    def validate(self) -> None:
        if not self.worker_values or any(value < 1 for value in self.worker_values):
            raise ValueError("worker_values must contain positive integers")
        if self.repeats < 2:
            raise ValueError("repeats must be at least 2")
        if self.warmups < 0:
            raise ValueError("warmups must be non-negative")


DEFAULT_SCENARIOS: dict[str, tuple[float, ...]] = {
    "balanced_io": (100.0, 100.0, 100.0, 100.0),
    "straggler_io": (20.0, 40.0, 80.0, 160.0),
}
ROLES = ("temporal", "topology", "configuration", "security")


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[position]


def _registry(delays_ms: Sequence[float]) -> tuple[SkillRegistry, list[RoleAssignment]]:
    if len(delays_ms) != len(ROLES):
        raise ValueError(f"expected {len(ROLES)} role delays")
    registry = SkillRegistry()
    assignments: list[RoleAssignment] = []
    for index, (role, delay_ms) in enumerate(zip(ROLES, delays_ms)):
        name = f"inspect_{role}"

        def handler(case, *, current_role=role, current_delay=delay_ms, current_index=index):
            time.sleep(current_delay / 1_000.0)
            return SkillResult(
                skill_name=f"inspect_{current_role}",
                evidence=[{
                    "evidence_id": f"ev-{current_index}",
                    "source": current_role,
                    "summary": f"controlled evidence from {current_role}",
                }],
                cost=0.25,
            )

        registry.register(
            SkillSpec(
                name=name,
                description=f"read-only {role} evidence",
                tags=[role],
                risk="read_only",
                cost=0.25,
            ),
            handler,
        )
        assignments.append(RoleAssignment(role=role, skill_names=(name,)))
    return registry, assignments


def _peak_overlap(findings: Sequence[RoleFinding]) -> int:
    points: list[tuple[float, int]] = []
    for finding in findings:
        points.append((finding.started_at, 1))
        points.append((finding.finished_at, -1))
    active = 0
    peak = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def run_scenario(
    name: str,
    delays_ms: Sequence[float],
    config: ParallelBenchmarkConfig,
) -> dict[str, Any]:
    config.validate()
    registry, assignments = _registry(delays_ms)
    executor = ParallelExecutorAgent()
    case = SimpleNamespace(id=f"benchmark-{name}")
    expected_ids = {f"ev-{index}" for index in range(len(assignments))}
    rows: list[dict[str, Any]] = []

    for workers in config.worker_values:
        for _ in range(config.warmups):
            executor.run(case, assignments, registry, max_workers=workers)

        latencies_ms: list[float] = []
        observed_peak = 0
        for _ in range(config.repeats):
            started = time.perf_counter()
            evidence, cost, findings = executor.run(
                case,
                assignments,
                registry,
                max_workers=workers,
            )
            latencies_ms.append((time.perf_counter() - started) * 1_000.0)
            observed_peak = max(observed_peak, _peak_overlap(findings))
            if {item["evidence_id"] for item in evidence} != expected_ids:
                raise AssertionError("parallelism changed the evidence set")
            if cost != len(assignments) * 0.25:
                raise AssertionError("parallelism changed tool cost")

        rows.append({
            "workers": workers,
            "p50_ms": round(statistics.median(latencies_ms), 4),
            "p95_ms": round(_percentile(latencies_ms, 0.95), 4),
            "p99_ms": round(_percentile(latencies_ms, 0.99), 4),
            "mean_ms": round(statistics.mean(latencies_ms), 4),
            "observed_peak_overlap": observed_peak,
            "evidence_count": len(expected_ids),
            "tool_cost": len(assignments) * 0.25,
        })

    serial = next(row for row in rows if row["workers"] == 1)
    for row in rows:
        row["p95_speedup_vs_serial"] = round(serial["p95_ms"] / row["p95_ms"], 4)
        usable_workers = min(row["workers"], len(assignments))
        row["parallel_efficiency"] = round(
            row["p95_speedup_vs_serial"] / usable_workers,
            4,
        )
    return {
        "scenario": name,
        "role_delays_ms": list(delays_ms),
        "same_work_at_every_concurrency": True,
        "rows": rows,
    }


def run_benchmark(
    config: ParallelBenchmarkConfig = ParallelBenchmarkConfig(),
    scenarios: dict[str, Sequence[float]] | None = None,
) -> dict[str, Any]:
    config.validate()
    selected = scenarios or DEFAULT_SCENARIOS
    started = time.perf_counter()
    return {
        "schema_version": 1,
        "benchmark": "same-work-multiagent-parallel-execution",
        "scope": "orchestration and I/O overlap only; excludes model quality and remote provider variance",
        "config": asdict(config),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "results": [run_scenario(name, delays, config) for name, delays in selected.items()],
        "total_seconds": round(time.perf_counter() - started, 4),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_benchmark(ParallelBenchmarkConfig(
        worker_values=tuple(args.workers),
        repeats=args.repeats,
        warmups=args.warmups,
    ))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(args.output)
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
