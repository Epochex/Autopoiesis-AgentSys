from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel

from core.eval.replay import ReplayMetrics, run_and_evaluate_replay
from core.trace.ledger import JSONLTraceLedger
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


class ContextComparisonRow(BaseModel):
    """One compiler strategy measured over the same replay evaluation path."""

    strategy: str
    reasoner_mode: str = "rule"
    dataset_kind: str
    split: str
    cases: int
    context_packets: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    compression_ratio: float
    included_memory_items: int
    included_evidence_items: int
    root_cause_accuracy: float
    evidence_recall: float
    citation_verify_pass_rate: float


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
        ("selfevo_light_path", {}, "memory + compressed context + skill controller"),
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


def compare_context_strategies(
    cases: list[RCASeedCase],
    ground_truth: dict[str, RCAGroundTruth],
    *,
    data_source: str = "mock",
    real_stats_path: str | Path | None = None,
) -> list[ContextComparisonRow]:
    """Measure legacy flat and structured context on identical cases.

    Token totals come from persisted ``context_compiled`` trace events. Quality
    comes from the same replay scorer used by the published ablation, so this
    comparison does not add an LLM judge or any online ground-truth access. It
    intentionally always uses the deterministic rule reasoner. That reasoner
    consumes raw evidence rather than the compiled summary, so equal quality is
    a regression check here, not evidence that compression improves accuracy.
    """
    if not cases:
        return []
    kinds = sorted({ground_truth[case.id].dataset_kind for case in cases if case.id in ground_truth})
    splits = sorted({ground_truth[case.id].split for case in cases if case.id in ground_truth})
    dataset_kind = ",".join(kinds) if kinds else "unknown"
    split = ",".join(splits) if splits else "unknown"

    rows: list[ContextComparisonRow] = []
    with TemporaryDirectory() as tmp_dir:
        for strategy in ("flat", "structured"):
            ledger_path = Path(tmp_dir) / f"context_{strategy}.jsonl"
            orchestrator = build_network_rca_orchestrator(
                ledger_path,
                reasoner_mode="rule",
                data_source=data_source,
                real_stats_path=real_stats_path,
                context_strategy=strategy,
            )
            metrics = run_and_evaluate_replay(orchestrator, cases, ground_truth)
            packets = [
                event.payload
                for event in JSONLTraceLedger(ledger_path).replay()
                if event.kind == "context_compiled"
            ]
            before = sum(int(packet.get("estimated_tokens_before", 0)) for packet in packets)
            after = sum(int(packet.get("estimated_tokens_after", 0)) for packet in packets)
            rows.append(
                ContextComparisonRow(
                    strategy=strategy,
                    reasoner_mode="rule",
                    dataset_kind=dataset_kind,
                    split=split,
                    cases=metrics.cases,
                    context_packets=len(packets),
                    estimated_tokens_before=before,
                    estimated_tokens_after=after,
                    compression_ratio=round(after / before, 4) if before else 0.0,
                    included_memory_items=sum(len(packet.get("included_memory_ids", [])) for packet in packets),
                    included_evidence_items=sum(len(packet.get("included_evidence_ids", [])) for packet in packets),
                    root_cause_accuracy=metrics.root_cause_accuracy,
                    evidence_recall=metrics.evidence_recall,
                    citation_verify_pass_rate=metrics.verifier_pass_rate,
                )
            )
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
