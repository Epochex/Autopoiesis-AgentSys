from __future__ import annotations

from domains.network_rca.investigation_case import (
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)
from frontend.gateway.app import investigate, main


def test_case_bound_session_survives_process_memory_loss(tmp_path, monkeypatch) -> None:
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    case = repository.ingest(CaseObservation(
        source=SourceReference("alert", "alert-session"),
        occurred_at="2026-08-29T10:00:00+00:00",
        subject="r230",
        rule_id="resource-pressure",
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    monkeypatch.setattr(investigate, "BASELINE_PROBES", [])
    monkeypatch.setattr(investigate, "TRIAGE_PROBES", [])

    opened = investigate.start(
        "调查 r230 资源异常",
        subject="r230",
        case_id=case.case_id,
    )
    session_id = opened["session_id"]
    investigate.get(session_id).collect_observation(
        label="test:resource_snapshot",
        payload={"cpu": 82, "memory_mb": 3900},
    )
    investigate.get(session_id).retrieval_results = [{
        "kind": "incident_dossier",
        "item_id": "dossier-1",
        "summary": "r230 previously showed sustained resource pressure",
        "source": "operational_memory",
        "locator": "operational-memory:incident_dossier:dossier-1",
        "route": ["scope_and_recency_rank", "exact_asset"],
        "score": 1.0,
        "matched_terms": ["r230"],
        "relation_to_current": {"matched_on": ["subject"]},
        "selected_for_context": True,
    }]
    investigate._persist_session(investigate.get(session_id))

    investigate._SESSIONS.pop(session_id)
    recovered = investigate.get(session_id)
    durable_case = repository.get(case.case_id)

    assert recovered.case_id == case.case_id
    assert recovered.evidence[-1]["command"] == "test:resource_snapshot"
    assert durable_case is not None
    assert durable_case.status == "investigating"
    assert any(
        event.get("sessionId") == session_id and event["kind"] == "evidence_collected"
        for event in durable_case.timeline
    )

    retrieval = investigate.live_retrieval_trace("调查 r230")
    assert retrieval["dataMode"] == "live_investigation_receipts"
    assert retrieval["cases"][0]["id"] == f"investigate:{session_id}"
    assert retrieval["cases"][0]["docs"]["dossier-1"]["source"].startswith(
        "operational_memory"
    )
