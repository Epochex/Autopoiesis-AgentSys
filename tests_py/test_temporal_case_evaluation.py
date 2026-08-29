from __future__ import annotations

from core.eval.temporal_case_evaluation import evaluate_temporal_cases


def _steps(*, memory: bool) -> list[dict]:
    rows = [
        {
            "sequence": 1,
            "state_version": 1,
            "process_generation": 1,
            "event_type": "probe",
            "probe_key": "inspect_gateway",
            "memory_ids": ["mem-same-device"] if memory else [],
        },
        {
            "sequence": 2,
            "state_version": 2,
            "process_generation": 1,
            "event_type": "hypothesis",
            "hypothesis": "gateway_failure",
        },
    ]
    if not memory:
        rows.append(
            {
                "sequence": 3,
                "state_version": 3,
                "process_generation": 1,
                "event_type": "probe",
                "probe_key": "inspect_gateway",
            }
        )
    offset = 1 if not memory else 0
    rows.extend(
        [
            {
                "sequence": 3 + offset,
                "state_version": 3 + offset,
                "process_generation": 2,
                "checkpoint_restored": True,
                "event_type": "evidence",
                "evidence_id": "ev-gateway-healthy",
            },
            {
                "sequence": 4 + offset,
                "state_version": 4 + offset,
                "process_generation": 2,
                "event_type": "wait",
            },
            {
                "sequence": 5 + offset,
                "state_version": 5 + offset,
                "process_generation": 2,
                "event_type": "evidence",
                "evidence_id": "ev-switch-loop",
            },
            {
                "sequence": 6 + offset,
                "state_version": 6 + offset,
                "process_generation": 2,
                "event_type": "hypothesis",
                "hypothesis": "switch_loop",
            },
            {
                "sequence": 7 + offset,
                "state_version": 7 + offset,
                "process_generation": 2,
                "event_type": "close",
                "hypothesis": "switch_loop",
            },
        ]
    )
    return rows


def _payload() -> dict:
    return {
        "cases": [
            {
                "case_id": "delayed-contradiction-restart",
                "expected_root_cause": "switch_loop",
                "false_lead_root_cause": "gateway_failure",
                "contradiction_step": 3,
                "decisive_evidence_step": 5,
                "runs": [
                    {"mode": "no_memory", "steps": _steps(memory=False)},
                    {"mode": "with_memory", "steps": _steps(memory=True)},
                ],
            }
        ]
    }


def test_temporal_metrics_cover_delay_contradiction_and_restart() -> None:
    result = evaluate_temporal_cases(_payload())

    for mode in ("no_memory", "with_memory"):
        summary = result["by_mode"][mode]
        assert summary["stable_root_cause_rate"] == 1.0
        assert summary["premature_close_rate"] == 0.0
        assert summary["contradiction_revision_rate"] == 1.0
        assert summary["restart_recovery_rate"] == 1.0
        assert summary["state_continuity_rate"] == 1.0


def test_memory_ablation_reports_cost_delta_without_inventing_quality_gain() -> None:
    result = evaluate_temporal_cases(_payload())
    ablation = result["memory_ablation"]

    assert ablation["paired_case_count"] == 1
    assert ablation["stable_root_cause_rate_delta"] == 0.0
    assert ablation["mean_probe_count_delta"] == -1.0
    assert ablation["mean_repeated_probe_count_delta"] == -1.0
    assert ablation["mean_steps_to_stable_root_delta"] == -1.0


def test_restart_without_checkpoint_fails_continuity_metric() -> None:
    payload = _payload()
    payload["cases"][0]["runs"][0]["steps"][3]["checkpoint_restored"] = False
    result = evaluate_temporal_cases(payload)

    assert result["by_mode"]["no_memory"]["restart_recovery_rate"] == 0.0
    assert result["by_mode"]["no_memory"]["state_continuity_rate"] == 0.0


def test_close_before_decisive_evidence_is_counted_as_premature() -> None:
    payload = _payload()
    payload["cases"][0]["runs"][0]["steps"] = [
        {
            "sequence": 1,
            "state_version": 1,
            "process_generation": 1,
            "event_type": "hypothesis",
            "hypothesis": "gateway_failure",
        },
        {
            "sequence": 2,
            "state_version": 2,
            "process_generation": 1,
            "event_type": "close",
            "hypothesis": "gateway_failure",
        },
    ]
    result = evaluate_temporal_cases(payload)

    assert result["by_mode"]["no_memory"]["premature_close_rate"] == 1.0
    assert result["by_mode"]["no_memory"]["stable_root_cause_rate"] == 0.0
