"""Replay-based evaluation: score runs purely from the persisted trace ledger.

Every diagnosis is reconstructed from replayed ``TraceEvent``s, never from live
state, so any past run can be re-scored offline and the numbers are reproducible
from the JSONL artifact alone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol, Sequence

from pydantic import BaseModel

from core.trace.ledger import JSONLTraceLedger


class GroundTruthLike(Protocol):
    """Ground-truth attributes the replay scorer needs (structural)."""

    expected_root_cause_key: str
    required_evidence: list[str]


class ReplayMetrics(BaseModel):
    cases: int
    root_cause_accuracy: float
    evidence_recall: float
    verifier_pass_rate: float


def run_replay(orchestrator, cases: Sequence[object], ground_truth: Mapping[str, GroundTruthLike]) -> ReplayMetrics:
    """Back-compat alias for :func:`run_and_evaluate_replay`."""
    return run_and_evaluate_replay(orchestrator, cases, ground_truth)


def run_and_evaluate_replay(orchestrator, cases: Sequence[object], ground_truth: Mapping[str, GroundTruthLike]) -> ReplayMetrics:
    """Diagnose every case, then score exclusively from the persisted ledger."""
    for case in cases:
        orchestrator.diagnose(case)
    return evaluate_trace(orchestrator.ledger.path, ground_truth)


def evaluate_trace(ledger_path: str | Path, ground_truth: Mapping[str, GroundTruthLike]) -> ReplayMetrics:
    """Score a trace ledger against ground truth.

    For a case diagnosed more than once, the latest events win (re-runs supersede).
    A case absent from the ledger counts as incorrect / unverified; a truth with no
    required evidence scores full recall. All ratios are 0.0 when ground_truth is
    empty rather than raising.
    """
    events = JSONLTraceLedger(ledger_path).replay()
    diagnoses = {
        event.case_id: event.payload
        for event in events
        if event.kind == "diagnosis_completed"
    }
    verifier = {
        event.case_id: event.payload
        for event in events
        if event.kind == "verifier_result"
    }
    correct = 0
    recall_total = 0.0
    passed = 0
    total = len(ground_truth)
    for case_id, truth in ground_truth.items():
        diagnosis = diagnoses.get(case_id, {})
        cited = {item["evidence_id"] for item in diagnosis.get("evidence", [])}
        required = set(truth.required_evidence)
        correct += int(diagnosis.get("root_cause_key") == truth.expected_root_cause_key)
        recall_total += len(required.intersection(cited)) / len(required) if required else 1.0
        passed += int(verifier.get(case_id, {}).get("passed") is True)
    return ReplayMetrics(
        cases=total,
        root_cause_accuracy=round(correct / total, 4) if total else 0.0,
        evidence_recall=round(recall_total / total, 4) if total else 0.0,
        verifier_pass_rate=round(passed / total, 4) if total else 0.0,
    )
