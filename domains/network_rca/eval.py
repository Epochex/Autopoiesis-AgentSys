from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel

from core.eval.replay import ReplayMetrics, run_and_evaluate_replay
from domains.network_rca.factory import build_network_rca_orchestrator
from domains.network_rca.schema import RCAGroundTruth, RCASeedCase


class BaselineRow(BaseModel):
    name: str
    dataset_kind: str
    split: str
    cases: int
    root_cause_accuracy: float
    evidence_recall: float
    verifier_pass_rate: float
    notes: str = ""


def compare_baselines(
    cases: list[RCASeedCase],
    ground_truth: dict[str, RCAGroundTruth],
    *,
    reasoner_mode: str = "rule",
    data_source: str = "mock",
    real_stats_path: str | Path | None = None,
) -> list[BaselineRow]:
    if not cases:
        return []
    kinds = sorted({ground_truth[case.id].dataset_kind for case in cases if case.id in ground_truth})
    splits = sorted({ground_truth[case.id].split for case in cases if case.id in ground_truth})
    dataset_kind = ",".join(kinds) if kinds else "unknown"
    split = ",".join(splits) if splits else "unknown"

    configs = [
        ("autopoiesis_light_path", {}, "memory + compressed context + skill controller"),
        ("full_context", {"context_enabled": False}, "no context compression"),
        ("full_tools", {"skill_controller_enabled": False, "top_k": 99}, "all readonly skills exposed"),
        ("no_memory", {"memory_enabled": False}, "memory retrieval disabled"),
    ]
    rows: list[BaselineRow] = []
    with TemporaryDirectory() as tmp_dir:
        for name, kwargs, notes in configs:
            ledger_path = Path(tmp_dir) / f"{name}.jsonl"
            orchestrator = build_network_rca_orchestrator(
                ledger_path,
                reasoner_mode=reasoner_mode,
                data_source=data_source,
                real_stats_path=real_stats_path,
                **kwargs,
            )
            metrics = run_and_evaluate_replay(orchestrator, cases, ground_truth)
            rows.append(_row(name, dataset_kind, split, metrics, notes))
    return rows


def _row(name: str, dataset_kind: str, split: str, metrics: ReplayMetrics, notes: str) -> BaselineRow:
    return BaselineRow(
        name=name,
        dataset_kind=dataset_kind,
        split=split,
        cases=metrics.cases,
        root_cause_accuracy=metrics.root_cause_accuracy,
        evidence_recall=metrics.evidence_recall,
        verifier_pass_rate=metrics.verifier_pass_rate,
        notes=notes,
    )
