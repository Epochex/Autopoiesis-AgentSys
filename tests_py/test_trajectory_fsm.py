from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from frontend.gateway.app import main
from frontend.gateway.app.investigation_session_store import InvestigationSessionStore
from frontend.gateway.app.trajectory_fsm import (
    analyze_trajectory,
    build_trajectory_model,
    extract_activity_sequence,
    trajectory_fsm_report,
)


def _session(
    session_id: str,
    activities: list[str],
    *,
    case_id: str = "case-1",
    state: str = "resolved",
) -> dict:
    return {
        "session_id": session_id,
        "case_id": case_id,
        "opened_at": f"2026-09-01T00:00:{session_id[-1]}0+00:00",
        "trace_events": [
            {"kind": activity, "at": "2026-09-01T00:00:00+00:00", "payload": {}}
            for activity in activities
        ],
        "decision": {"state": state, "classification": "verified"},
    }


def test_model_is_deterministic_and_preserves_transition_probabilities():
    rows = [
        _session("session-1", ["start probe", "hypothesisConfirmed"]),
        _session("session-2", ["start-probe", "hypothesis_rejected"]),
        _session("session-3", ["start_probe", "hypothesis_confirmed"]),
    ]

    first = build_trajectory_model(rows, context_window=2)
    second = build_trajectory_model(list(reversed(rows)), context_window=2)

    assert first == second
    assert first["alphabet"] == [
        "hypothesis_confirmed",
        "hypothesis_rejected",
        "start_probe",
    ]
    transitions = {
        (row["source_state"], row["activity"]): row
        for row in first["transitions"]
    }
    confirmed = transitions[("state:start_probe", "hypothesis_confirmed")]
    rejected = transitions[("state:start_probe", "hypothesis_rejected")]
    assert confirmed["count"] == 2
    assert confirmed["probability"] == 0.666667
    assert rejected["count"] == 1
    assert rejected["probability"] == 0.333333


def test_cycle_rate_detects_revisited_merged_state():
    target = _session(
        "session-cycle",
        ["probe_selected", "tool_called", "probe_selected", "tool_called"],
    )

    analysis = analyze_trajectory(target, [], context_window=2)

    assert analysis["cycle_edges"] == 1
    assert analysis["cycle_rate"] == 0.25
    assert analysis["early_stop_enabled"] is False
    assert analysis["shadow_mode"] is True


def test_target_session_is_excluded_so_an_unseen_transition_remains_visible():
    historical = [_session("session-1", ["probe_selected", "tool_called"])]
    target = _session("session-2", ["probe_selected", "verifier_result"])

    report = trajectory_fsm_report(
        [*historical, target], session_id="session-2", context_window=2
    )

    unseen = report["analysis"]["unseen_transition"]
    assert unseen["unseen"] is True
    assert unseen["activity"] == "verifier_result"
    assert unseen["historical_count"] == 0
    assert report["model"]["samples"]["sessions"] == 1
    assert report["analysis"]["calibration_boundary"]["target_session_excluded"]


def test_failure_signal_is_an_uncalibrated_score_with_reasons():
    training = [
        _session("session-1", ["probe_selected", "tool_called"], state="resolved"),
        _session("session-2", ["probe_selected", "tool_called"], state="failed"),
    ]
    target = _session("session-3", ["probe_selected", "tool_called"])

    risk = analyze_trajectory(target, training)["failure_risk"]

    assert risk["failure_signal"] == "elevated"
    assert risk["risk_score"] == 0.5
    assert risk["calibrated"] is False
    assert risk["state_session_samples"] == 2
    assert risk["reasons"][0] == "historical_failed_or_unresolved_signals=1/2"


def test_empty_data_and_empty_session_have_explicit_sample_boundaries():
    summary = build_trajectory_model([])
    analysis = analyze_trajectory(_session("session-0", []), [])

    assert summary["samples"] == {
        "sessions": 0,
        "nonempty_sessions": 0,
        "activities": 0,
        "transitions": 0,
        "failed_or_unresolved_sessions": 0,
    }
    assert summary["alphabet"] == []
    assert summary["cycle_rate"] == 0.0
    assert analysis["current_state"]["state_id"] == "start"
    assert analysis["failure_risk"]["risk_score"] is None
    assert analysis["failure_risk"]["failure_signal"] == "unseen_state"
    assert analysis["failure_risk"]["calibrated"] is False
    assert analysis["failure_risk"]["state_session_samples"] == 0
    assert analysis["unseen_transition"]["reason"] == "session_has_no_valid_activities"


def test_legacy_schema_activity_fields_are_normalised():
    snapshot = {
        "sessionId": "legacy-session",
        "caseId": "legacy-case",
        "traceEvents": [
            {"eventKind": "Probe Selected"},
            {"type": "tool-called"},
            {"payload": {"action": "VerifierResult"}},
            "Diagnosis Completed",
            {"at": "2026-09-01T00:00:00Z"},
        ],
    }

    assert extract_activity_sequence(snapshot) == (
        "probe_selected",
        "tool_called",
        "verifier_result",
        "diagnosis_completed",
    )


def test_read_only_api_supports_summary_session_and_case_queries(monkeypatch, tmp_path):
    store = InvestigationSessionStore(tmp_path)
    store.save(_session("session-1", ["probe_selected", "tool_called"], case_id="case-a"))
    store.save(_session("session-2", ["probe_selected", "verifier_result"], case_id="case-a"))
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, investigation_session_store_dir=tmp_path),
    )
    client = TestClient(main.app)

    summary = client.get("/api/rca/investigate/trajectory-fsm")
    session = client.get(
        "/api/rca/investigate/trajectory-fsm", params={"session_id": "session-2"}
    )
    case = client.get(
        "/api/rca/investigate/trajectory-fsm", params={"case_id": "case-a"}
    )
    missing = client.get(
        "/api/rca/investigate/trajectory-fsm", params={"session_id": "missing"}
    )

    assert summary.status_code == 200
    assert summary.json()["mode"] == "summary"
    assert summary.json()["model"]["samples"]["sessions"] == 2
    assert summary.json()["sessionCount"] == 2
    assert summary.json()["eventCount"] == 4
    assert summary.json()["trainedAt"]
    assert summary.json()["limits"] == {
        "maxSessions": 1000,
        "maxEventsPerSession": 500,
        "truncatedSessions": 0,
    }
    assert session.status_code == 200
    assert session.json()["analysis"]["session_id"] == "session-2"
    assert session.json()["analysis"]["early_stop_enabled"] is False
    assert case.status_code == 200
    assert case.json()["selector"]["matching_sessions"] == 2
    assert missing.status_code == 404

    cached = client.get("/api/rca/investigate/trajectory-fsm")
    assert cached.json()["trainedAt"] == summary.json()["trainedAt"]


def test_event_limit_preserves_the_trailing_current_state():
    target = _session("session-long", ["first", "middle", "last"])

    report = trajectory_fsm_report(
        [target],
        session_id="session-long",
        max_events_per_session=2,
    )

    assert report["analysis"]["activities"] == ["middle", "last"]
    assert report["analysis"]["current_state"]["context"] == ["middle", "last"]
    assert report["limits"]["truncated_sessions"] == 1
