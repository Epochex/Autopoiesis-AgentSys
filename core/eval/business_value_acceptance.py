"""Compute the six business-value acceptance results from executed case records.

This report does not infer product value from code presence or unit-test names.
Each row names the durable event or session state that must exist in an actual
case.  An empty eligible population stays ``not_observed``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from domains.network_rca.business_decision import is_terminal_local_in_deny


def _case_id(case: Mapping[str, Any]) -> str:
    return str(case.get("caseId") or case.get("case_id") or "")


def _timeline(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in case.get("timeline") or () if isinstance(item, Mapping)]


def _latest_decision(case: Mapping[str, Any]) -> dict[str, Any]:
    direct = case.get("businessDecision")
    if isinstance(direct, Mapping):
        return dict(direct)
    event = next(
        (item for item in reversed(_timeline(case)) if item.get("kind") == "business_decision_recorded"),
        None,
    )
    return dict((event or {}).get("decision") or {})


def _confirmed_decision_is_grounded(
    case: Mapping[str, Any],
    session: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    """Check that the published root is supported by the cited current bytes."""
    classification = str(decision.get("classification") or "")
    cited = {
        str(item.get("evidenceId") or "")
        for item in decision.get("evidence") or ()
        if isinstance(item, Mapping) and item.get("evidenceId")
    }
    hypotheses = list(dict(session.get("hypothesis_state") or {}).get("hypotheses") or ())
    root = next(
        (
            item for item in hypotheses
            if item.get("hypothesis_id") == classification
            and item.get("status") == "confirmed"
        ),
        None,
    )
    if root is None:
        return False
    supporting = {str(value) for value in root.get("supporting_evidence_ids") or ()}
    opposing = {str(value) for value in root.get("opposing_evidence_ids") or ()}
    if not cited or not supporting or not supporting.issubset(cited) or opposing:
        return False
    evidence_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in session.get("evidence") or ()
        if isinstance(item, Mapping) and item.get("evidence_id")
    }
    if any(evidence_id not in evidence_by_id for evidence_id in supporting):
        return False
    extra_citations = cited - supporting
    if any(
        str(dict(evidence_by_id.get(evidence_id) or {}).get("source") or "")
        != "action_readback"
        for evidence_id in extra_citations
    ):
        return False
    if root.get("origin") != "model":
        return True
    if extra_citations:
        return False
    supports = [evidence_by_id.get(evidence_id) for evidence_id in sorted(supporting)]
    if any(item is None for item in supports):
        return False
    claims = [dict(item.get("claim_support") or {}) for item in supports if item is not None]
    signal_families = {
        str(claim.get("signalFamily") or "") for claim in claims
        if claim.get("signalFamily")
    }
    return bool(
        len(claims) >= 2
        and len(signal_families) >= 2
        and all(
            claim.get("hypothesisId") == classification
            and claim.get("matched") is True
            and claim.get("frozenBeforeProbe") is True
            for claim in claims
        )
    )


def _row(
    *,
    key: str,
    eligible: int,
    passed: int,
    case_ids: Sequence[str],
    measured: Mapping[str, Any],
    requirement: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "status": "proven" if eligible > 0 and passed == eligible else (
            "partial" if passed > 0 else ("failed" if eligible > 0 else "not_observed")
        ),
        "eligibleCases": eligible,
        "passedCases": passed,
        "caseIds": list(dict.fromkeys(case_ids))[:20],
        "measured": dict(measured),
        "requirement": requirement,
    }


def _has_probe_headroom(report: Mapping[str, Any]) -> bool:
    """A first-probe confirmation has no possible probe-count improvement."""
    fixed = dict(report.get("fixed_script") or report.get("control") or {})
    step = fixed.get("steps_to_first_confirmation")
    return isinstance(step, int) and step > 1


def _recurrence_has_probe_headroom(report: Mapping[str, Any]) -> bool:
    recurrence = dict(report.get("recurrence") or {})
    prior = dict(recurrence.get("prior") or {})
    probe_count = prior.get("probe_count")
    return isinstance(probe_count, int) and probe_count > 1


def _speed_report_is_proven(report: Mapping[str, Any]) -> bool:
    comparison = dict(report.get("fixed_script_comparison") or {})
    fixed = dict(report.get("fixed_script") or {})
    treatment = dict(report.get("treatment") or {})
    fixed_probes = fixed.get("probe_count")
    treatment_probes = treatment.get("probe_count")
    fixed_step = fixed.get("steps_to_first_confirmation")
    treatment_step = treatment.get("steps_to_first_confirmation")
    fixed_elapsed = fixed.get("elapsed_ms")
    treatment_elapsed = treatment.get("elapsed_ms")
    return bool(
        comparison.get("business_value_proven") is True
        and comparison.get("same_confirmed_root") is True
        and comparison.get("same_probe_coverage") is True
        and comparison.get("comparable_probe_outputs") is True
        and comparison.get("wall_time_measurable") is True
        and comparison.get("lower_wall_time") is True
        and comparison.get("fewer_executed_probes") is True
        and isinstance(fixed_probes, int)
        and isinstance(treatment_probes, int)
        and treatment_probes < fixed_probes
        and isinstance(fixed_step, int)
        and isinstance(treatment_step, int)
        and treatment_step < fixed_step
        and isinstance(fixed_elapsed, (int, float))
        and isinstance(treatment_elapsed, (int, float))
        and treatment_elapsed < fixed_elapsed
    )


def _recurrence_report_is_proven(report: Mapping[str, Any]) -> bool:
    recurrence = dict(report.get("recurrence") or {})
    prior = dict(recurrence.get("prior") or {})
    current = dict(recurrence.get("current") or {})
    prior_probes = prior.get("probe_count")
    current_probes = current.get("probe_count")
    prior_step = prior.get("steps_to_first_confirmation")
    current_step = current.get("steps_to_first_confirmation")
    acceptance = dict(report.get("acceptance") or {})
    stable = dict(report.get("stable_roots_by_strategy") or {})
    return bool(
        report.get("recurrence_value_proven") is True
        and acceptance.get("business_value_proven") is True
        and acceptance.get("comparable_probe_outputs") is True
        and recurrence.get("same_confirmed_root") is True
        and recurrence.get("fewer_probes_than_first_incident") is True
        and recurrence.get("earlier_confirmation_than_first_incident") is True
        and isinstance(prior_probes, int)
        and isinstance(current_probes, int)
        and current_probes < prior_probes
        and isinstance(prior_step, int)
        and isinstance(current_step, int)
        and current_step < prior_step
        and stable
        and all(value is True for value in stable.values())
    )


def evaluate_business_value(
    cases: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    *,
    acceptance_only: bool = False,
) -> dict[str, Any]:
    """Return value claims backed only by completed case/session artifacts."""

    supplied_cases = list(cases)
    acceptance_runs = sorted({
        str(dict(case.get("sourcePayload") or {}).get("acceptanceRunId") or "")
        for case in supplied_cases
        if dict(case.get("sourcePayload") or {}).get("acceptanceRunId")
    })
    latest_acceptance_run = acceptance_runs[-1] if acceptance_runs else None
    superseded_acceptance_count = sum(
        1
        for case in supplied_cases
        if (
            dict(case.get("sourcePayload") or {}).get("acceptanceRunId")
            and str(dict(case.get("sourcePayload") or {}).get("acceptanceRunId"))
            != latest_acceptance_run
        )
    )
    production_excluded_count = sum(
        1
        for case in supplied_cases
        if not dict(case.get("sourcePayload") or {}).get("acceptanceRunId")
    ) if acceptance_only else 0
    cases = [
        case for case in supplied_cases
        if (
            str(dict(case.get("sourcePayload") or {}).get("acceptanceRunId") or "")
            == latest_acceptance_run
            if acceptance_only
            else (
                not dict(case.get("sourcePayload") or {}).get("acceptanceRunId")
                or str(dict(case.get("sourcePayload") or {}).get("acceptanceRunId"))
                == latest_acceptance_run
            )
        )
    ]
    active_case_ids = {_case_id(case) for case in cases}
    sessions = [
        session for session in sessions
        if str(session.get("case_id") or "") in active_case_ids
    ]

    sessions_by_case: dict[str, list[Mapping[str, Any]]] = {}
    for session in sessions:
        case_id = str(session.get("case_id") or "")
        if case_id:
            sessions_by_case.setdefault(case_id, []).append(session)

    auto_eligible: list[str] = []
    auto_passed: list[str] = []
    open_eligible: list[str] = []
    open_passed: list[str] = []
    grounded_eligible: list[str] = []
    grounded_passed: list[str] = []
    recovery_eligible: list[str] = []
    recovery_passed: list[str] = []
    pair_eligible: list[str] = []
    pair_passed: list[str] = []
    recurrence_eligible: list[str] = []
    recurrence_passed: list[str] = []
    probe_deltas: list[int] = []
    speed_no_headroom = 0
    speed_no_baseline_confirmation = 0
    recurrence_no_headroom = 0
    recurrence_no_baseline_confirmation = 0

    for case in cases:
        case_id = _case_id(case)
        timeline = _timeline(case)
        case_sessions = sessions_by_case.get(case_id, [])
        started = [
            event for event in timeline
            if event.get("kind") == "investigation_session_started"
            and event.get("autoStarted") is True
        ]
        if started:
            auto_eligible.append(case_id)
            if any(
                event.get("scopeQuality") in {"exact", "partial"}
                and event.get("faultDomain")
                and event.get("managedAssets")
                for event in started
            ):
                auto_passed.append(case_id)

        for session in case_sessions:
            hypotheses = list(dict(session.get("hypothesis_state") or {}).get("hypotheses") or ())
            model_roots = [item for item in hypotheses if item.get("origin") == "model"]
            if model_roots:
                open_eligible.append(case_id)
                decision = dict(session.get("decision") or {})
                if any(
                    item.get("status") == "confirmed"
                    and decision.get("classification") == item.get("hypothesis_id")
                    for item in model_roots
                ) and _confirmed_decision_is_grounded(case, session, decision):
                    open_passed.append(case_id)

        decision = _latest_decision(case)
        classification = str(decision.get("classification") or "")
        if classification and classification not in {
            "incident_scope_unresolved", "open_root_required", "root_cause_unresolved",
            "policy_outcome_unresolved",
        }:
            grounded_eligible.append(case_id)
            if classification == "blocked_external_probe":
                facts = dict(case.get("sourcePayload") or {}).get("incidentFacts") or {}
                if decision.get("evidence") and is_terminal_local_in_deny(dict(facts)):
                    grounded_passed.append(case_id)
            else:
                for session in case_sessions:
                    if _confirmed_decision_is_grounded(case, session, decision):
                        grounded_passed.append(case_id)
                        break

        action_ready = any(
            event.get("kind") == "business_decision_recorded"
            and dict(event.get("decision") or {}).get("state") == "action_ready"
            for event in timeline
        )
        if action_ready:
            recovery_eligible.append(case_id)
            final = _latest_decision(case)
            readback = dict(final.get("readback") or {})
            if final.get("state") in {"resolved", "escalated"} and readback.get("outcome"):
                recovery_passed.append(case_id)

        pair_reports = [
            dict(event.get("report") or {})
            for event in timeline
            if event.get("kind") in {
                "investigation_pair_measured", "memory_value_measured",
            }
            and isinstance(event.get("report"), Mapping)
        ]
        speed_reports = [report for report in pair_reports if _has_probe_headroom(report)]
        speed_no_headroom += sum(
            dict(report.get("fixed_script") or report.get("control") or {}).get(
                "steps_to_first_confirmation"
            ) == 1
            for report in pair_reports
        )
        speed_no_baseline_confirmation += sum(
            not isinstance(
                dict(report.get("fixed_script") or report.get("control") or {}).get(
                    "steps_to_first_confirmation"
                ),
                int,
            )
            for report in pair_reports
        )
        if speed_reports:
            pair_eligible.append(case_id)
            if any(
                _speed_report_is_proven(report)
                for report in speed_reports
            ):
                pair_passed.append(case_id)

        recurrence_reports = [
            dict(event.get("report") or {})
            for event in timeline
            if event.get("kind") == "memory_value_measured"
            and isinstance(event.get("report"), Mapping)
        ]
        eligible_recurrences = [
            report for report in recurrence_reports
            if _recurrence_has_probe_headroom(report)
        ]
        recurrence_no_headroom += sum(
            dict(dict(report.get("recurrence") or {}).get("prior") or {}).get(
                "probe_count"
            ) == 1
            for report in recurrence_reports
        )
        recurrence_no_baseline_confirmation += sum(
            not isinstance(
                dict(dict(report.get("recurrence") or {}).get("prior") or {}).get(
                    "probe_count"
                ),
                int,
            )
            for report in recurrence_reports
        )
        if eligible_recurrences:
            recurrence_eligible.append(case_id)
        for report in eligible_recurrences:
            if _recurrence_report_is_proven(report):
                recurrence_passed.append(case_id)
                delta = dict(report.get("recurrence") or {}).get("probe_delta")
                if isinstance(delta, int):
                    probe_deltas.append(delta)

    rows = [
        _row(
            key="automatic_incident_takeover",
            eligible=len(set(auto_eligible)),
            passed=len(set(auto_passed)),
            case_ids=auto_passed,
            measured={"scopedAutoStartedCases": len(auto_passed)},
            requirement="auto-started case has a managed asset, bounded scope and fault domain",
        ),
        _row(
            key="open_fault_investigation",
            eligible=len(set(open_eligible)),
            passed=len(set(open_passed)),
            case_ids=open_passed,
            measured={"confirmedModelOriginRoots": len(open_passed)},
            requirement="model-origin root is confirmed by frozen independent probes and becomes the decision",
        ),
        _row(
            key="grounded_decisions",
            eligible=len(set(grounded_eligible)),
            passed=len(set(grounded_passed)),
            case_ids=grounded_passed,
            measured={
                "groundedDecisionRate": (
                    round(len(set(grounded_passed)) / len(set(grounded_eligible)), 4)
                    if grounded_eligible else None
                )
            },
            requirement="final evidence ids are the deterministic supports of the published root",
        ),
        _row(
            key="faster_investigation",
            eligible=len(set(pair_eligible)),
            passed=len(set(pair_passed)),
            case_ids=pair_passed,
            measured={
                "sameCasePairsWithEarlierConfirmation": len(set(pair_passed)),
                "excludedFirstProbeBaselines": speed_no_headroom,
                "excludedUnconfirmedBaselines": speed_no_baseline_confirmation,
            },
            requirement=(
                "same current root under equal candidate coverage, with fewer executed probes "
                "and lower measured wall time than the fixed-script control"
            ),
        ),
        _row(
            key="action_and_recovery_readback",
            eligible=len(set(recovery_eligible)),
            passed=len(set(recovery_passed)),
            case_ids=recovery_passed,
            measured={"actionsWithTerminalReadback": len(set(recovery_passed))},
            requirement="action-ready case ends resolved or escalated with original-system readback",
        ),
        _row(
            key="recurrence_memory_value",
            eligible=len(set(recurrence_eligible)),
            passed=len(set(recurrence_passed)),
            case_ids=recurrence_passed,
            measured={
                "recurrencesWithProvenSavings": len(set(recurrence_passed)),
                "probeDeltas": probe_deltas,
                "excludedFirstProbeBaselines": recurrence_no_headroom,
                "excludedUnconfirmedBaselines": recurrence_no_baseline_confirmation,
            },
            requirement="recurrence matches the first root and uses fewer probes; same-case ablation also passes",
        ),
    ]
    return {
        "allProven": all(row["status"] == "proven" for row in rows),
        "rows": rows,
        "caseCount": len(cases),
        "sessionCount": len(sessions),
        "cohortPolicy": {
            "acceptanceOnly": acceptance_only,
            "latestAcceptanceRunId": latest_acceptance_run,
            "supersededAcceptanceCasesExcluded": superseded_acceptance_count,
            "productionCasesExcluded": production_excluded_count,
        },
    }


__all__ = ["evaluate_business_value"]
