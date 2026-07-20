"""Drive the agent over a *stream* of events and measure self-evolution.

The online path stays a single-agent read-only diagnosis; between events the
consolidation step (offline) evolves the persistent core. On a stream of
recurring real incidents the agent should handle later occurrences with fewer
probes / lower cost (via the procedural-memory shortcut) at unchanged accuracy —
that gap, cold-vs-warm, is the honest, measurable self-evolution signal.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping, Protocol, Sequence

from core.evolve.consolidate import CaseLike, _first, consolidate_run
from core.evolve.memory_ops import memory_health, utility_evict
from core.evolve.observatory import CAPABILITIES, recall_row, serialize_store
from domains.network_rca.factory import build_network_rca_orchestrator


class GroundTruthLike(Protocol):
    """The ground-truth attribute the stream scorer needs (structural)."""

    expected_root_cause_key: str


def run_evolving_stream(
    cases: Sequence[CaseLike],
    ground_truth: Mapping[str, GroundTruthLike],
    *,
    passes: int = 3,
    evolve: bool = True,
    data_source: str = "mock",
    real_stats_path: str | Path | None = None,
    reasoner_mode: str = "rule",
    capacity_budget: int | None = None,
    resolve_conflicts: bool = False,
) -> dict:
    """Process `cases` `passes` times in order (recurring incidents over time).

    Returns per-event metrics + a per-pass summary. Memory starts EMPTY so the
    only thing that changes across passes is what the agent has learned.

    `capacity_budget` (when set) caps the warm store: after each consolidation the
    lowest-*utility* memories are evicted back to the budget (utility_evict) — this is
    what makes eviction actually fire. `resolve_conflicts` turns on the SUPERSEDE path
    so a re-diagnosis that renames the root cause retires the stale prior.
    """
    with TemporaryDirectory() as tmp:
        orch = build_network_rca_orchestrator(
            Path(tmp) / "stream.jsonl",
            data_source=data_source,
            real_stats_path=real_stats_path,
            reasoner_mode=reasoner_mode,
            seed_memory=False,
        )
        n = len(cases)
        stream = [case for _ in range(passes) for case in cases]
        per_event: list[dict] = []
        # observability side-channel (warm runs only): the item-level memory lifecycle
        # the aggregate metrics above throw away. Collected, never acted on.
        obs_events: list[dict] = []
        obs_recall: list[dict] = []
        obs_reports: list[dict] = []
        for i, case in enumerate(stream):
            diagnosis, report = orch.diagnose(case)
            events = list(orch._run_events)
            probes = sum(1 for e in events if e.kind == "tool_called" and not e.payload.get("blocked"))
            cost_ev = _first(events, "cost_observed")
            cost = float(cost_ev.payload.get("tool_cost", 0.0)) if cost_ev else 0.0
            mem_read = _first(events, "memory_read")
            retrieved = sum(len(v) for v in mem_read.payload.values()) if mem_read else 0
            shortcut = any(e.kind == "memory_shortcut" for e in events)
            resolved = any(e.kind == "memory_resolved" for e in events)
            truth = ground_truth.get(case.id)
            correct = int(bool(truth) and diagnosis.root_cause_key == truth.expected_root_cause_key)
            active_mem = len(orch.memory.active())
            per_event.append({
                "i": i, "pass": i // n, "case": case.id,
                "probes": probes, "cost": round(cost, 2), "retrieved": retrieved,
                "shortcut": shortcut, "resolved": resolved, "correct": correct,
                "passed": int(bool(report.passed)), "memory": active_mem,
            })
            if evolve:
                run_id = orch.last_run_id
                ops: list[dict] = []
                report = consolidate_run(
                    events, case, orch.memory, orch.skills, orch._last_evidence,
                    resolve_conflicts=resolve_conflicts, recorder=ops,
                )
                if capacity_budget is not None:
                    # capacity-budgeted utility eviction — the wired firing point. EVICT
                    # ops flow into the same observability stream as every other op.
                    utility_evict(orch.memory, budget=capacity_budget, recorder=ops)
                obs_recall.append(recall_row(
                    events, seq=len(obs_recall), pass_no=i // n,
                    case_id=case.id, run_id=run_id, probes=probes,
                ))
                for op in ops:
                    obs_events.append({
                        "seq": len(obs_events), "pass": i // n,
                        "case_id": case.id, "run_id": run_id, **op,
                    })
                # the ConsolidationReport was previously discarded here; it is the
                # kernel's own audit trail, so surface it alongside the op stream.
                obs_reports.append({
                    "seq": len(obs_reports), "pass": i // n, "case_id": case.id,
                    "run_id": report.run_id, "passed": report.passed,
                    "added": list(report.added), "updated": list(report.updated),
                    "superseded": list(report.superseded),
                    "reinforced": list(report.reinforced), "quarantined": list(report.quarantined),
                    "linked": list(report.linked), "insights": list(report.insights),
                    "skills_success": list(report.skills_success),
                    "skills_misuse": list(report.skills_misuse),
                    "skills_frozen": list(report.skills_frozen),
                })
        out = {
            "per_event": per_event,
            "by_pass": _by_pass(per_event, passes),
            "final_memory": per_event[-1]["memory"] if per_event else 0,
            "memory_health": memory_health(orch.memory),
        }
        if evolve:
            # cold runs carry evolve=False and therefore have no memory lifecycle at all.
            out["observatory"] = {
                "records": serialize_store(orch.memory),
                "events": obs_events,
                "recall": obs_recall,
                "reports": obs_reports,
                "capabilities": dict(CAPABILITIES),
            }
        return out


def _by_pass(per_event: list[dict], passes: int) -> list[dict]:
    out = []
    for p in range(passes):
        rows = [e for e in per_event if e["pass"] == p]
        if not rows:
            continue
        out.append({
            "pass": p,
            "probes": sum(r["probes"] for r in rows),
            "cost": round(sum(r["cost"] for r in rows), 2),
            "shortcuts": sum(r["shortcut"] for r in rows),
            "recalled": sum(r["resolved"] for r in rows),
            "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 4),
            "verify": round(sum(r["passed"] for r in rows) / len(rows), 4),
            "memory_end": rows[-1]["memory"],
        })
    return out


def compare_cold_vs_warm(
    cases: Sequence[CaseLike],
    ground_truth: Mapping[str, GroundTruthLike],
    *,
    passes: int = 3,
    **kwargs,
) -> dict:
    """The headline experiment: same recurring stream, evolution on vs off."""
    warm = run_evolving_stream(cases, ground_truth, passes=passes, evolve=True, **kwargs)
    cold = run_evolving_stream(cases, ground_truth, passes=passes, evolve=False, **kwargs)
    n_warm = len(warm["per_event"])
    n_cold = len(cold["per_event"])
    warm_probes = sum(e["probes"] for e in warm["per_event"])
    cold_probes = sum(e["probes"] for e in cold["per_event"])
    warm_cost = round(sum(e["cost"] for e in warm["per_event"]), 2)
    cold_cost = round(sum(e["cost"] for e in cold["per_event"]), 2)
    warm_acc = round(sum(e["correct"] for e in warm["per_event"]) / n_warm, 4) if n_warm else 0.0
    cold_acc = round(sum(e["correct"] for e in cold["per_event"]) / n_cold, 4) if n_cold else 0.0
    return {
        "warm": warm, "cold": cold,
        "memory": warm.get("memory_health", {}),
        "delta": {
            "probes_cold": cold_probes, "probes_warm": warm_probes,
            "probes_saved_pct": round((cold_probes - warm_probes) / cold_probes * 100, 1) if cold_probes else 0.0,
            "cost_cold": cold_cost, "cost_warm": warm_cost,
            "cost_saved_pct": round((cold_cost - warm_cost) / cold_cost * 100, 1) if cold_cost else 0.0,
            "accuracy_cold": cold_acc, "accuracy_warm": warm_acc,
            "memory_grown": warm["final_memory"],
        },
    }
