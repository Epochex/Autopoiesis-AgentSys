from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.memory.operational_repository import InMemoryOperationalRepository
from frontend.gateway.app import investigate, main
from frontend.gateway.app.operational_memory import OperationalMemoryService


def _session() -> investigate.Session:
    at = datetime(2026, 8, 23, 12, tzinfo=timezone.utc).isoformat()
    session = investigate.Session(
        session_id="close-session",
        question="gateway carrier is down",
        family="fam-host-config-drift",
        subject="192.168.1.27",
        opened_at=at,
    )
    session.evidence = [{
        "evidence_id": "ev-001",
        "command": "ip -br link show",
        "output": "eth2 DOWN",
        "ok": True,
        "at": at,
    }]
    session.analysis_citations = ["ev-001"]
    investigate._SESSIONS[session.session_id] = session
    return session


def test_operator_confirmation_creates_idempotent_authoritative_dossier(monkeypatch):
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)
    monkeypatch.setattr(main, "_operational_memory", service)
    session = _session()

    first = investigate.close(
        session.session_id,
        resolution="confirmed",
        root_cause="physical carrier loss on eth2",
        confirmed_by="operator-7",
        evidence_ids=["ev-001"],
        operator_note="checked switch port",
    )
    second = investigate.close(
        session.session_id,
        resolution="confirmed",
        root_cause="physical carrier loss on eth2",
        confirmed_by="operator-7",
        evidence_ids=["ev-001"],
        operator_note="checked switch port",
    )

    assert first["dossier"]["dossier_id"] == "investigate:close-session"
    assert second["dossier"]["dossier_id"] == first["dossier"]["dossier_id"]
    dossier = service.dossiers.get("investigate:close-session")
    assert dossier is not None
    assert dossier.root_causes[0].status == "confirmed"
    assert dossier.root_causes[0].confirmed_by == "operator-7"
    assert len(service.repository.load("incident_dossier")) == 1


def test_confirmation_without_fresh_evidence_is_rejected(monkeypatch):
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)
    monkeypatch.setattr(main, "_operational_memory", service)
    session = _session()
    session.analysis_citations = []

    with pytest.raises(ValueError, match="requires fresh session evidence"):
        investigate.close(
            session.session_id,
            resolution="confirmed",
            root_cause="physical carrier loss on eth2",
            confirmed_by="operator-7",
        )


def test_verified_remediation_closes_dossier_and_feeds_action_effect(monkeypatch):
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)
    monkeypatch.setattr(main, "_operational_memory", service)
    session = _session()
    investigate.close(
        session.session_id,
        resolution="confirmed",
        root_cause="physical carrier loss on eth2",
        confirmed_by="operator-7",
        evidence_ids=["ev-001"],
    )
    opened = service.dossiers.get("investigate:close-session")
    assert opened is not None
    completed = opened.updated_at + timedelta(minutes=10)
    result = {
        "at": completed.isoformat(),
        "action": "reset_link",
        "target": "192.168.1.27",
        "outcome": "passed",
        "verdict": {
            "outcome": "passed",
            "window_seconds": 300,
            "samples": [{
                "probe": "carrier",
                "at": completed.isoformat(),
                "reading": {"carrier": 1},
                "healthy": True,
                "regressed": False,
            }],
        },
    }

    payload = service.attach_remediation_run("investigate:close-session", result)

    assert payload["status"] == "resolved"
    assert payload["remediation_attempts"][0]["observation"]["verdict"] == "passed"
    assert service.repository.get("incident_dossier", "investigate:close-session").version == 2
    statements = {feature.statement for feature in service.features.store.features()}
    assert "root_cause:physical carrier loss on eth2" in statements
    assert "action_effect:reset_link" in statements
