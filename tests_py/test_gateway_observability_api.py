from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from domains.network_rca.factory import build_network_rca_service, load_seed_cases

pytest.importorskip("fastapi", reason="gateway extra is not installed")
from frontend.gateway.app import main as gateway


class _SerializableResult:
    def __init__(self, **payload):
        self._payload = payload
        for name, value in payload.items():
            setattr(self, name, value)

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return dict(self._payload)


def test_gateway_does_not_report_full_success_when_memory_commit_failed(monkeypatch):
    class _PartialService:
        last_consolidation = None
        last_run_id = "run-partial"
        _run_events: list[object] = []

        def diagnose(self, _case, *, session_id=None):
            assert session_id == "incident-partial"
            return (
                _SerializableResult(root_cause_key="carrier_down"),
                _SerializableResult(passed=True),
            )

        def health(self):
            return {"last_error": "RuntimeError: durable memory commit failed"}

    # The endpoint accepts only cases already validated by the server-side map.
    case = SimpleNamespace(id="case-partial")
    monkeypatch.setattr(gateway, "_evolving_service", _PartialService())
    monkeypatch.setattr(gateway, "_diagnosis_cases", {case.id: case})

    response = asyncio.run(
        gateway.rca_diagnose(
            gateway.RCADiagnosisRequest(
                case_id=case.id,
                session_id="incident-partial",
            )
        )
    )

    assert response["diagnosisVerified"] is True
    assert response["memoryCommitted"] is False
    assert response["ok"] is False
    assert response["overallStatus"] == "partial_failure"


def test_gateway_exposes_trace_detail_and_cross_run_session(monkeypatch, tmp_path):
    service = build_network_rca_service(
        tmp_path / "business.jsonl",
        observability_path=tmp_path / "nodes.jsonl",
        seed_memory=False,
        start_maintenance=False,
    )
    case = load_seed_cases()[0]
    service.diagnose(case, session_id="incident-api")
    first_run_id = service.last_run_id
    service.diagnose(case, session_id="incident-api")

    monkeypatch.setattr(gateway, "_evolving_service", service)

    recent = gateway.rca_observation_traces(limit=10, session_id="incident-api")
    detail = gateway.rca_observation_trace(first_run_id)
    session = gateway.rca_observation_session("incident-api")

    assert len(recent["traces"]) == 2
    assert detail["trace_id"] == first_run_id
    assert detail["nodes"][0]["node_name"] == "rca.evolving_run"
    assert session["trace_count"] == 2
    assert len(session["evolution_series"]) == 2
    assert "performance_by_node" in session
    assert service.close()
