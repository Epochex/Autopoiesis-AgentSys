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
    comparison = {
        "business_value_proven": True,
        "same_confirmed_root": True,
        "same_probe_coverage": True,
        "comparable_probe_outputs": True,
        "wall_time_measurable": True,
        "lower_wall_time": True,
        "fewer_executed_probes": True,
    }
    pair = {
        "acceptance": comparison,
        "fixed_script_comparison": comparison,
        "fixed_script": {
            "probe_count": 6,
            "steps_to_first_confirmation": 3,
            "elapsed_ms": 300,
        },
        "treatment": {
            "probe_count": 1,
            "steps_to_first_confirmation": 1,
            "elapsed_ms": 100,
        },
        "recurrence_value_proven": True,
        "stable_roots_by_strategy": {
            "fixed_script": True,
            "no_memory": True,
            "full_system": True,
        },
        "recurrence": {
            "same_confirmed_root": True,
            "fewer_probes_than_first_incident": True,
            "earlier_confirmation_than_first_incident": True,
            "probe_delta": 5,
            "prior": {"probe_count": 6, "steps_to_first_confirmation": 3},
            "current": {"probe_count": 1, "steps_to_first_confirmation": 1},
        },
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
        "evidence": [{"evidence_id": "ev-service", "output": "failed"}],
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
    assert rows["recurrence_memory_value"]["measured"]["probeDeltas"] == [5]
    assert rows["open_fault_investigation"]["status"] == "not_observed"


def test_model_root_needs_two_matched_frozen_signal_families() -> None:
    decision = {
        "state": "escalated",
        "classification": "model:dependency",
        "evidence": [{"evidenceId": "ev-log"}, {"evidenceId": "ev-endpoint"}],
    }
    case = {"caseId": "case-open", "businessDecision": decision, "timeline": []}
    session = {
        "case_id": "case-open",
        "decision": decision,
        "hypothesis_state": {
            "hypotheses": [{
                "hypothesis_id": "model:dependency",
                "origin": "model",
                "status": "confirmed",
                "supporting_evidence_ids": ["ev-log", "ev-endpoint"],
                "opposing_evidence_ids": [],
            }]
        },
        "evidence": [
            {
                "evidence_id": "ev-log",
                "claim_support": {
                    "hypothesisId": "model:dependency",
                    "signalFamily": "service_logs",
                    "matched": True,
                    "frozenBeforeProbe": True,
                },
            },
            {
                "evidence_id": "ev-endpoint",
                "claim_support": {
                    "hypothesisId": "model:dependency",
                    "signalFamily": "endpoint_state",
                    "matched": True,
                    "frozenBeforeProbe": True,
                },
            },
        ],
    }

    rows = {
        row["key"]: row for row in evaluate_business_value([case], [session])["rows"]
    }
    assert rows["open_fault_investigation"]["status"] == "proven"
    assert rows["grounded_decisions"]["status"] == "proven"

    session["evidence"] = session["evidence"][:1]
    rows = {
        row["key"]: row for row in evaluate_business_value([case], [session])["rows"]
    }
    assert rows["open_fault_investigation"]["status"] == "failed"
    assert rows["grounded_decisions"]["status"] == "failed"

    session["evidence"].append({
        "evidence_id": "ev-endpoint",
        "claim_support": {
            "hypothesisId": "model:dependency",
            "signalFamily": "endpoint_state",
            "matched": True,
            "frozenBeforeProbe": True,
        },
    })
    session["evidence"][1]["claim_support"]["signalFamily"] = "service_logs"
    rows = {
        row["key"]: row for row in evaluate_business_value([case], [session])["rows"]
    }
    assert rows["open_fault_investigation"]["status"] == "failed"
    assert rows["grounded_decisions"]["status"] == "failed"


def test_manual_pair_proves_speed_without_claiming_recurrence() -> None:
    pair = {
        "fixed_script_comparison": {
            "business_value_proven": True,
            "same_confirmed_root": True,
            "same_probe_coverage": True,
            "comparable_probe_outputs": True,
            "wall_time_measurable": True,
            "lower_wall_time": True,
            "fewer_executed_probes": True,
        },
        "fixed_script": {
            "probe_count": 4,
            "steps_to_first_confirmation": 3,
            "elapsed_ms": 400,
        },
        "treatment": {
            "probe_count": 1,
            "steps_to_first_confirmation": 1,
            "elapsed_ms": 100,
        },
    }
    case = {
        "caseId": "case-pair",
        "timeline": [{"kind": "investigation_pair_measured", "report": pair}],
    }

    report = evaluate_business_value([case], [])
    rows = {row["key"]: row for row in report["rows"]}

    assert rows["faster_investigation"]["status"] == "proven"
    assert rows["recurrence_memory_value"]["status"] == "not_observed"


def test_unconfirmed_baseline_is_not_a_speed_sample() -> None:
    case = {
        "caseId": "case-no-baseline-root",
        "timeline": [{
            "kind": "investigation_pair_measured",
            "report": {
                "fixed_script": {"steps_to_first_confirmation": None},
                "fixed_script_comparison": {"business_value_proven": False},
            },
        }],
    }

    rows = {
        row["key"]: row for row in evaluate_business_value([case], [])["rows"]
    }

    speed = rows["faster_investigation"]
    assert speed["status"] == "not_observed"
    assert speed["measured"]["excludedUnconfirmedBaselines"] == 1


def test_only_latest_controlled_acceptance_run_contributes() -> None:
    cases = [
        {
            "caseId": "old-failed",
            "sourcePayload": {"acceptanceRunId": "20260831T100000Z-old"},
            "timeline": [{
                "kind": "investigation_session_started",
                "autoStarted": True,
                "scopeQuality": "unresolved",
            }],
        },
        {
            "caseId": "latest-passed",
            "sourcePayload": {"acceptanceRunId": "20260831T110000Z-new"},
            "timeline": [{
                "kind": "investigation_session_started",
                "autoStarted": True,
                "scopeQuality": "exact",
                "faultDomain": "asset:host-a",
                "managedAssets": ["host-a"],
            }],
        },
        {
            "caseId": "production-case",
            "sourcePayload": {},
            "timeline": [],
        },
    ]

    report = evaluate_business_value(cases, [], acceptance_only=True)
    takeover = next(
        row for row in report["rows"] if row["key"] == "automatic_incident_takeover"
    )

    assert takeover["status"] == "proven"
    assert takeover["eligibleCases"] == 1
    assert report["cohortPolicy"]["supersededAcceptanceCasesExcluded"] == 1
    assert report["cohortPolicy"]["productionCasesExcluded"] == 1
    assert report["cohortPolicy"]["acceptanceOnly"] is True


def test_production_cohort_excludes_controlled_acceptance_cases() -> None:
    cases = [
        {
            "caseId": "controlled",
            "sourcePayload": {"acceptanceRunId": "run-1"},
            "timeline": [{
                "kind": "investigation_session_started",
                "autoStarted": True,
                "scopeQuality": "exact",
                "faultDomain": "asset:test",
                "managedAssets": ["test"],
            }],
        },
        {
            "caseId": "production",
            "sourcePayload": {},
            "timeline": [],
        },
    ]

    report = evaluate_business_value(cases, [], production_only=True)

    assert report["caseCount"] == 1
    assert report["cohortPolicy"]["productionOnly"] is True
    assert next(
        row for row in report["rows"] if row["key"] == "automatic_incident_takeover"
    )["status"] == "not_observed"


def test_sentinel_action_is_grounded_and_recovery_is_counted_from_one_case_chain() -> None:
    evidence_id = "sev-current"
    action_ready = {
        "state": "action_ready",
        "classification": "service_failed",
        "evidence": [{"evidenceId": evidence_id}],
    }
    final = {
        "state": "resolved",
        "classification": "service_failed",
        "evidence": [{"evidenceId": evidence_id}],
        "readback": {"outcome": "passed"},
    }
    case = {
        "caseId": "case-sentinel",
        "sourcePayload": {
            "dataClassification": "observed",
            "incidentFacts": {"sentinelEvidenceId": evidence_id},
        },
        "businessDecision": final,
        "timeline": [
            {"kind": "business_decision_recorded", "decision": action_ready},
            {"kind": "remediation_started"},
            {"kind": "remediation_completed", "outcome": "passed"},
            {"kind": "business_decision_recorded", "decision": final},
        ],
    }

    rows = {
        row["key"]: row for row in evaluate_business_value([case], [], production_only=True)["rows"]
    }

    assert rows["grounded_decisions"]["status"] == "proven"
    assert rows["action_and_recovery_readback"]["status"] == "proven"
