from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.memory.store import MemoryRecord
from domains.network_rca.incident_dossier import from_sentinel_chain
from domains.network_rca.incident_memory import synthesize_incident_run
from frontend.gateway.app import main as gateway
from tests_py.test_incident_memory import _passed_chain, _safety_gated_chain


@pytest.fixture
def inline_thread(monkeypatch):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(gateway.asyncio, "to_thread", run_inline)


def _operational(chain: list[dict]):
    dossier = from_sentinel_chain(chain, source_mode="live")
    return SimpleNamespace(
        incident_receipt_view=lambda **_kwargs: {
            "ok": True,
            "durable": True,
            "dossiers": [{
                "id": dossier.dossier_id,
                "title": dossier.fault_summary,
                "status": dossier.status,
                "source": "live",
                "evidence_count": len(dossier.evidence),
            }],
            "risks": [],
            "features": [],
        }
    )


def test_passed_incident_receipt_lists_only_records_from_that_chain(
    monkeypatch, inline_thread,
):
    chain = _passed_chain()
    run = synthesize_incident_run(chain)
    assert run is not None
    current = MemoryRecord(
        memory_id="proc-sentinel.failed_units",
        tier="procedural",
        text="verified procedure",
        asset_ids=["demo-api.service"],
        source_trace_ids=[run.run_id],
    )
    old = MemoryRecord(
        memory_id="old-related",
        tier="episodic",
        text="older case",
        asset_ids=["demo-api.service"],
        source_trace_ids=["older-run"],
    )
    monkeypatch.setattr(gateway, "_memory_records", lambda: (True, [current, old], None, None))
    monkeypatch.setattr(gateway, "_completed_memory_chain", lambda *_args: chain)
    monkeypatch.setattr(gateway, "_memory_trace_events", lambda: [])
    monkeypatch.setattr(gateway, "_memory_influence_timeline", lambda: [])
    monkeypatch.setattr(gateway, "_operational_memory", _operational(chain))

    response = asyncio.run(gateway.rca_event_memory_receipt(
        subject="demo-api.service", incident_ref="sentinel:exact",
    ))

    assert response["lifecycle"] == "memory_committed"
    assert response["latest_incident"]["source_trace_id"] == run.run_id
    assert [row["memory_id"] for row in response["current_memories"]] == [current.memory_id]
    assert response["related_memories"] == []
    assert len(response["dossiers"]) == 1


def test_safety_gated_receipt_has_dossier_and_no_successful_procedure(
    monkeypatch, inline_thread,
):
    chain = _safety_gated_chain()
    run_id = gateway._incident_memory_trace_id(chain)
    assert run_id is not None
    retained = MemoryRecord(
        memory_id="epi-safety-demo",
        tier="episodic",
        text="safety gate retained",
        asset_ids=["203.0.113.77"],
        source_trace_ids=[run_id],
    )
    monkeypatch.setattr(gateway, "_memory_records", lambda: (True, [retained], None, None))
    monkeypatch.setattr(gateway, "_completed_memory_chain", lambda *_args: chain)
    monkeypatch.setattr(gateway, "_memory_trace_events", lambda: [])
    monkeypatch.setattr(gateway, "_memory_influence_timeline", lambda: [])
    monkeypatch.setattr(gateway, "_operational_memory", _operational(chain))

    response = asyncio.run(gateway.rca_event_memory_receipt(
        subject="203.0.113.77", incident_ref="sentinel:exact",
    ))

    assert response["lifecycle"] == "safety_gated"
    assert response["latest_incident"]["terminal_kind"] == "no_safe_action"
    assert [row["tier"] for row in response["current_memories"]] == ["episodic"]
    assert response["dossiers"][0]["status"] == "escalated"
