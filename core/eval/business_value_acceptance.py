"""Compute the six business-value acceptance results from executed case records.

This report does not infer product value from code presence or unit-test names.
Each row names the durable event or session state that must exist in an actual
case.  An empty eligible population stays ``not_observed``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


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


def evaluate_business_value(
    cases: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return value claims backed only by completed case/session artifacts."""

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
                ):
                    open_passed.append(case_id)

        decision = _latest_decision(case)
        classification = str(decision.get("classification") or "")
        if classification and classification not in {
            "incident_scope_unresolved", "open_root_required", "root_cause_unresolved",
            "policy_outcome_unresolved",
        }:
            grounded_eligible.append(case_id)
            if classification == "blocked_external_probe":
                if decision.get("evidence"):
                    grounded_passed.append(case_id)
            else:
                for session in case_sessions:
                    hypotheses = list(
                        dict(session.get("hypothesis_state") or {}).get("hypotheses") or ()
                    )
                    root = next(
                        (
                            item for item in hypotheses
                            if item.get("hypothesis_id") == classification
                            and item.get("status") == "confirmed"
                        ),
                        None,
                    )
                    if root is None:
                        continue
                    cited = {
                        str(item.get("evidenceId") or "")
                        for item in decision.get("evidence") or ()
                        if item.get("evidenceId")
                    }
                    supporting = set(root.get("supporting_evidence_ids") or ())
                    if cited and cited.issubset(supporting):
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
        if pair_reports:
            pair_eligible.append(case_id)
            if any(
                dict(report.get("fixed_script_comparison") or {}).get(
                    "business_value_proven"
                ) is True
                for report in pair_reports
            ):
                pair_passed.append(case_id)

        recurrence_reports = [
            dict(event.get("report") or {})
            for event in timeline
            if event.get("kind") == "memory_value_measured"
            and isinstance(event.get("report"), Mapping)
        ]
        if recurrence_reports:
            recurrence_eligible.append(case_id)
        for report in recurrence_reports:
            if report.get("recurrence_value_proven") is True:
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
            measured={"sameCasePairsWithEarlierConfirmation": len(set(pair_passed))},
            requirement="same fresh probe outputs and root, with earlier confirmation than the fixed-script control",
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
            },
            requirement="recurrence matches the first root and uses fewer probes; same-case ablation also passes",
        ),
    ]
    return {
        "allProven": all(row["status"] == "proven" for row in rows),
        "rows": rows,
        "caseCount": len(cases),
        "sessionCount": len(sessions),
    }


__all__ = ["evaluate_business_value"]
