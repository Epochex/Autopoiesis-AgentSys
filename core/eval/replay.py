from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from core.trace.ledger import JSONLTraceLedger


class ReplayMetrics(BaseModel):
    cases: int
    root_cause_accuracy: float
    evidence_recall: float
    verifier_pass_rate: float


def run_replay(orchestrator, cases: list, ground_truth: dict[str, object]) -> ReplayMetrics:
    return run_and_evaluate_replay(orchestrator, cases, ground_truth)


def run_and_evaluate_replay(orchestrator, cases: list, ground_truth: dict[str, object]) -> ReplayMetrics:
    for case in cases:
        orchestrator.diagnose(case)
    return evaluate_trace(orchestrator.ledger.path, ground_truth)


def evaluate_trace(ledger_path: str | Path, ground_truth: dict[str, object]) -> ReplayMetrics:
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
