from __future__ import annotations

from core.eval.business_value_acceptance import evaluate_business_value


def test_empty_population_proves_nothing() -> None:
    report = evaluate_business_value([], [])

    assert report["allProven"] is False
    assert {row["status"] for row in report["rows"]} == {"not_observed"}


def test_report_counts_only_executed_case_artifacts() -> None:
    decision = {
        "state": "resolved",
        "classification": "service_failed",
        "evidence": [{"evidenceId": "ev-service"}],
        "readback": {"outcome": "passed"},
    }
    pair = {
        "acceptance": {"business_value_proven": True},
        "fixed_script_comparison": {"business_value_proven": True},
        "recurrence_value_proven": True,
        "recurrence": {"probe_delta": 3},
    }
    case = {
        "caseId": "case-1",
        "businessDecision": decision,
        "timeline": [
            {
                "kind": "investigation_session_started",
                "autoStarted": True,
                "scopeQuality": "exact",
                "faultDomain": "host-service:collector.service",
                "managedAssets": ["collector.service"],
            },
            {
                "kind": "business_decision_recorded",
                "decision": {"state": "action_ready"},
            },
            {"kind": "memory_value_measured", "report": pair},
            {"kind": "business_decision_recorded", "decision": decision},
        ],
    }
    session = {
        "case_id": "case-1",
        "decision": decision,
        "hypothesis_state": {
            "hypotheses": [
                {
                    "hypothesis_id": "service_failed",
                    "origin": "catalog",
                    "status": "confirmed",
                    "supporting_evidence_ids": ["ev-service"],
                }
            ]
        },
    }

    report = evaluate_business_value([case], [session])
    rows = {row["key"]: row for row in report["rows"]}

    assert rows["automatic_incident_takeover"]["status"] == "proven"
    assert rows["grounded_decisions"]["status"] == "proven"
    assert rows["faster_investigation"]["status"] == "proven"
    assert rows["action_and_recovery_readback"]["status"] == "proven"
    assert rows["recurrence_memory_value"]["measured"]["probeDeltas"] == [3]
    assert rows["open_fault_investigation"]["status"] == "not_observed"


def test_manual_pair_proves_speed_without_claiming_recurrence() -> None:
    pair = {"fixed_script_comparison": {"business_value_proven": True}}
    case = {
        "caseId": "case-pair",
        "timeline": [{"kind": "investigation_pair_measured", "report": pair}],
    }

    report = evaluate_business_value([case], [])
    rows = {row["key"]: row for row in report["rows"]}

    assert rows["faster_investigation"]["status"] == "proven"
    assert rows["recurrence_memory_value"]["status"] == "not_observed"
