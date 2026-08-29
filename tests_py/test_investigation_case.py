from __future__ import annotations

from domains.network_rca.investigation_case import (
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)
from frontend.gateway.app.investigation_cases import sync_snapshot_cases


def _repository(tmp_path) -> InvestigationCaseRepository:
    return InvestigationCaseRepository(tmp_path / "investigation-cases.sqlite3")


def test_repository_recovers_the_same_case_after_reconstruction(tmp_path) -> None:
    path = tmp_path / "investigation-cases.sqlite3"
    first = InvestigationCaseRepository(path)
    created = first.ingest(CaseObservation(
        source=SourceReference("alert", "alert-1"),
        occurred_at="2026-08-29T10:00:00+00:00",
        subject="r230",
        rule_id="deny-burst",
        summary="denied flow burst",
    ))

    recovered = InvestigationCaseRepository(path).get(created.case_id)

    assert recovered is not None
    assert recovered.case_id == created.case_id
    assert recovered.sources == (SourceReference("alert", "alert-1"),)
    assert recovered.status == "open"


def test_repeated_delivery_is_idempotent(tmp_path) -> None:
    repository = _repository(tmp_path)
    observation = CaseObservation(
        source=SourceReference("alert", "alert-1"),
        occurred_at="2026-08-29T10:00:00+00:00",
        subject="r230",
        rule_id="deny-burst",
        payload={"count": 12},
    )
    first = repository.ingest(observation)
    second = repository.ingest(observation)

    assert second.case_id == first.case_id
    assert second.occurrence_count == 1
    assert second.version == first.version
    assert second.sources == (SourceReference("alert", "alert-1"),)


def test_cluster_suggestion_merges_provisional_alert_cases(tmp_path) -> None:
    repository = _repository(tmp_path)
    alert_ids = ("alert-1", "alert-2", "alert-3")
    provisional_ids = {
        repository.ingest(CaseObservation(
            source=SourceReference("alert", alert_id),
            occurred_at=f"2026-08-29T10:00:0{index}+00:00",
            subject="r230",
            rule_id="deny-burst",
        )).case_id
        for index, alert_id in enumerate(alert_ids, start=1)
    }
    assert len(provisional_ids) == 3

    merged = repository.ingest(CaseObservation(
        source=SourceReference("suggestion", "suggestion-1"),
        occurred_at="2026-08-29T10:01:00+00:00",
        subject="r230",
        service="https",
        rule_id="deny-burst",
        scope="cluster",
        summary="three related denied-flow alerts",
        related_sources=tuple(SourceReference("alert", item) for item in alert_ids),
        hypotheses={"primaryHypothesisId": "hyp-1"},
        timeline=({"kind": "suggestion_emitted", "ts": "2026-08-29T10:01:00+00:00"},),
    ))

    assert len(repository.list()) == 1
    assert merged.occurrence_count == 3
    assert merged.latest_suggestion_id == "suggestion-1"
    assert merged.hypotheses["primaryHypothesisId"] == "hyp-1"
    assert all(
        repository.case_id_for(SourceReference("alert", alert_id)) == merged.case_id
        for alert_id in alert_ids
    )


def test_open_and_follow_up_events_are_durable_and_idempotent(tmp_path) -> None:
    repository = _repository(tmp_path)
    case = repository.ingest(CaseObservation(
        source=SourceReference("alert", "alert-1"),
        occurred_at="2026-08-29T10:00:00+00:00",
    ))

    opened = repository.open(case.case_id, actor="alice")
    assert opened is not None
    assert opened.status == "investigating"
    assert opened.timeline[-1]["kind"] == "case_opened"

    appended = repository.append_event(
        case.case_id,
        kind="probe_completed",
        payload={"sessionId": "session-1", "evidenceIds": ["ev-001"]},
        status="waiting",
        event_id="session-1:probe-1",
    )
    repeated = repository.append_event(
        case.case_id,
        kind="probe_completed",
        payload={"sessionId": "session-1", "evidenceIds": ["ev-001"]},
        status="waiting",
        event_id="session-1:probe-1",
    )

    assert appended is not None and repeated is not None
    assert repeated.status == "waiting"
    assert repeated.version == appended.version
    assert [item["kind"] for item in repeated.timeline].count("probe_completed") == 1


def test_repolled_suggestion_keeps_investigation_transitions(tmp_path) -> None:
    repository = _repository(tmp_path)
    observation = CaseObservation(
        source=SourceReference("suggestion", "suggestion-1"),
        occurred_at="2026-08-29T10:01:00+00:00",
        timeline=({"kind": "suggestion_emitted", "ts": "2026-08-29T10:01:00+00:00"},),
    )
    case = repository.ingest(observation)
    repository.open(case.case_id, actor="alice")

    repolled = repository.ingest(observation)

    assert repolled.status == "investigating"
    assert [event["kind"] for event in repolled.timeline] == [
        "suggestion_emitted", "case_opened",
    ]


def test_snapshot_projection_joins_alerts_and_suggestion_to_one_case(tmp_path) -> None:
    repository = _repository(tmp_path)
    snapshot = {
        "feed": [
            {
                "id": "feed-alert-alert-1", "sourceId": "alert-1", "kind": "alert",
                "ts": "2026-08-29T10:00:00+00:00", "deviceKey": "r230",
                "severity": "high", "ruleId": "deny-burst", "service": "https",
            },
            {
                "id": "feed-suggestion-suggestion-1", "kind": "suggestion",
                "ts": "2026-08-29T10:01:00+00:00",
            },
        ],
        "suggestions": [
            {
                "id": "suggestion-1", "ts": "2026-08-29T10:01:00+00:00",
                "scope": "cluster", "deviceKey": "r230", "severity": "high",
                "ruleId": "deny-burst", "service": "https", "summary": "clustered",
                "sourceAlertIds": ["alert-1"], "hypothesisSet": {}, "timeline": [],
            }
        ],
    }

    projected = sync_snapshot_cases(snapshot, repository)
    alert_case = projected["feed"][0]["caseId"]
    suggestion_case = projected["suggestions"][0]["caseId"]

    assert alert_case == suggestion_case
    assert projected["feed"][1]["caseId"] == suggestion_case
    assert len(repository.list()) == 1


def test_case_query_and_open_http_api(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from frontend.gateway.app import main

    repository = _repository(tmp_path)
    case = repository.ingest(CaseObservation(
        source=SourceReference("alert", "alert-api"),
        occurred_at="2026-08-29T10:00:00+00:00",
        subject="r230",
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    client = TestClient(main.app)

    listing = client.get("/api/rca/investigation-cases")
    detail = client.get(f"/api/rca/investigation-cases/{case.case_id}")
    opened = client.post(f"/api/rca/investigation-cases/{case.case_id}/open?actor=tester")

    assert listing.status_code == 200
    assert listing.json()["cases"][0]["caseId"] == case.case_id
    assert detail.status_code == 200
    assert detail.json()["case"]["sources"] == [
        {"kind": "alert", "sourceId": "alert-api"}
    ]
    assert opened.status_code == 200
    assert opened.json()["case"]["status"] == "investigating"
    assert opened.json()["case"]["timeline"][-1]["actor"] == "tester"
