from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.eval.active_investigation_benchmark import (
    REQUIRED_ADVERSARIAL_KINDS,
    VARIANTS,
    BenchmarkBundle,
    evaluate_active_investigation,
)


FIXTURE = Path(__file__).parent / "fixtures" / "active_investigation_adversarial.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _run(report: dict, case_id: str, variant: str) -> dict:
    return next(
        row
        for row in report["runs"]
        if row["case_id"] == case_id and row["variant"] == variant
    )


def test_controlled_fixture_covers_required_attacks_and_variants() -> None:
    payload = _payload()
    bundle = BenchmarkBundle.model_validate(payload)
    report = evaluate_active_investigation(payload)

    assert bundle.data_classification == "synthetic_controlled"
    assert bundle.benchmark_purpose == "evaluator_contract_smoke_test"
    assert tuple(bundle.variants) == VARIANTS
    assert report["case_count"] == 7
    assert set(report["adversarial_coverage"]) == REQUIRED_ADVERSARIAL_KINDS
    assert "do not establish live" in report["scope"]
    assert all(report["by_variant"][variant]["run_count"] == 7 for variant in VARIANTS)


def test_fixture_metrics_are_deterministic_and_hand_checkable() -> None:
    payload = _payload()
    first = evaluate_active_investigation(payload)
    second = evaluate_active_investigation(copy.deepcopy(payload))

    assert first == second
    assert first["by_variant"]["direct_api"]["decisive_evidence_recall_at_k"] == 0.0
    assert first["by_variant"]["direct_api"]["distractor_retrieval_rate"] == 1.0
    assert first["by_variant"]["direct_api"]["false_confirmation_rate"] == 1.0
    assert first["by_variant"]["hybrid_filtered"]["decisive_evidence_recall_at_k"] == 1.0
    assert first["by_variant"]["hybrid_filtered"]["root_cause_accuracy"] == 0.857143
    assert first["by_variant"]["full_active"]["decisive_evidence_recall_at_k"] == 0.916667
    assert first["by_variant"]["full_active"]["root_cause_accuracy"] == 0.714286
    assert first["by_variant"]["full_active"]["false_confirmation_rate"] == 0.0


def test_correct_guess_without_decisive_evidence_is_false_confirmation() -> None:
    report = evaluate_active_investigation(_payload())
    guessed = _run(report, "decisive-tool-timeout", "direct_api")
    refused = _run(report, "decisive-tool-timeout", "full_active")

    assert guessed["root_cause_accurate"] is True
    assert guessed["false_confirmation"] is True
    assert guessed["withheld_evidence_false_confirmation"] is True
    assert guessed["incorrect_action_count"] == 1
    assert refused["root_cause_accurate"] is False
    assert refused["false_confirmation"] is False
    assert refused["withheld_evidence_false_confirmation"] is False
    assert refused["incorrect_action_count"] == 0


def test_combined_fault_requires_all_roots_and_all_decisive_evidence() -> None:
    report = evaluate_active_investigation(_payload())
    hybrid = _run(report, "combined-route-and-service-fault", "hybrid_filtered")
    active = _run(report, "combined-route-and-service-fault", "full_active")

    assert hybrid["decisive_evidence_recall_at_k"] == 1.0
    assert hybrid["root_cause_accurate"] is True
    assert hybrid["restart_continuity"] is True
    assert active["decisive_evidence_recall_at_k"] == 0.5
    assert active["root_cause_accurate"] is False
    assert active["false_confirmation"] is False
    assert active["restart_continuity"] is True


def test_restart_and_repeated_probe_metrics_detect_lost_state() -> None:
    report = evaluate_active_investigation(_payload())
    direct = _run(report, "combined-route-and-service-fault", "direct_api")
    hybrid = _run(report, "combined-route-and-service-fault", "hybrid_filtered")

    assert direct["restart_continuity"] is False
    assert direct["probe_count"] == 2
    assert direct["repeated_probe_count"] == 1
    assert hybrid["restart_continuity"] is True
    assert hybrid["repeated_probe_count"] == 0


def test_paired_differences_use_case_aligned_candidate_minus_baseline() -> None:
    report = evaluate_active_investigation(_payload())
    comparison = report["paired_differences"]["full_active_vs_direct_api"]

    assert comparison["pair_count"] == 7
    assert comparison["direction"] == "candidate_minus_direct_api"
    assert comparison["metric_pair_counts"]["decisive_evidence_recall_at_k"] == 6
    assert comparison["metric_pair_counts"]["withheld_evidence_false_confirmation_rate"] == 1
    assert comparison["deltas"]["false_confirmation_rate_delta"] == -1.0
    assert comparison["deltas"]["root_cause_accuracy_delta"] == 0.571429
    assert comparison["deltas"]["mean_probe_count_delta"] == 1.0


def test_allowed_action_before_grounded_confirmation_is_incorrect() -> None:
    payload = _payload()
    case = payload["cases"][0]
    run = next(row for row in case["runs"] if row["variant"] == "hybrid_filtered")
    run["trace"].insert(
        3,
        {
            "sequence": 4,
            "state_version": 4,
            "process_generation": 1,
            "event_type": "action",
            "action_id": "restart_camera_service",
        },
    )
    for sequence, event in enumerate(run["trace"], start=1):
        event["sequence"] = sequence
        event["state_version"] = sequence

    report = evaluate_active_investigation(payload)
    row = _run(report, case["case_id"], "hybrid_filtered")
    assert row["incorrect_action_count"] == 1


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda payload: payload.update({"unexpected": True}), "Extra inputs"),
        (
            lambda payload: payload["cases"][0]["runs"].pop(),
            "each benchmark variant exactly once",
        ),
        (
            lambda payload: payload["cases"][0]["runs"][0]["trace"][0].update(
                {"evidence_id": "unknown"}
            ),
            "unknown evidence ids",
        ),
        (
            lambda payload: payload["cases"][5]["runs"][0]["trace"][0].update(
                {"evidence_id": "hidden-active-route-missing"}
            ),
            "withheld decisive evidence leaked",
        ),
    ],
)
def test_strict_schema_rejects_malformed_bundles(mutate, match: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(ValidationError, match=match):
        evaluate_active_investigation(payload)
