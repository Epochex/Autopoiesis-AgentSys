"""Memory influence is narrower than retrieval and links both directions."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.memory.store import MemoryRecord
from core.trace.events import TraceEvent
from frontend.gateway.app import main as gateway


class _Store:
    repository = None

    def __init__(self, records: list[MemoryRecord]):
        self._records = records

    def records(self) -> list[MemoryRecord]:
        return list(self._records)


class _Ledger:
    def __init__(self, events: list[TraceEvent]):
        self._events = events

    def replay(self) -> list[TraceEvent]:
        return list(self._events)


@pytest.fixture
def inline_thread(monkeypatch):
    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(gateway.asyncio, "to_thread", run_inline)


def _record(*, with_cycle: bool = False) -> MemoryRecord:
    evidence_snapshot = []
    if with_cycle:
        evidence_snapshot = [{
            "evidence_id": "sentinel-evidence:resolved",
            "source": "sentinel:resolved",
            "summary": "passed",
            "data": {
                "kind": "resolved",
                "at": "2026-08-22T10:01:36+00:00",
                "detector": "failed_units",
                "subject": "demo-api.service",
                "action": "restart_unit",
                "outcome": "passed",
                "samples": 12,
            },
        }]
    return MemoryRecord(
        memory_id="memory-a",
        tier="episodic",
        text="demo-api.service was restored by restart_unit",
        source_trace_ids=["origin-run", "access:decision-run", "credit:decision-run"],
        evidence_snapshot=evidence_snapshot,
    )


def _install(monkeypatch, record: MemoryRecord, events: list[TraceEvent]) -> None:
    monkeypatch.setattr(
        gateway,
        "_evolving_service",
        SimpleNamespace(
            memory=_Store([record]),
            orchestrator=SimpleNamespace(ledger=_Ledger(events)),
        ),
    )
    monkeypatch.setattr(gateway, "_memory_influence_timeline", lambda: [])


def _influence() -> dict:
    return asyncio.run(gateway.rca_memory_influence("memory-a"))


def test_unknown_memory_returns_404(monkeypatch, inline_thread):
    monkeypatch.setattr(
        gateway,
        "_evolving_service",
        SimpleNamespace(
            memory=_Store([]),
            orchestrator=SimpleNamespace(ledger=_Ledger([])),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(gateway.rca_memory_influence("missing"))

    assert exc_info.value.status_code == 404


def test_retrieval_alone_is_not_an_influence(monkeypatch, inline_thread):
    event = TraceEvent(
        run_id="read-only-run",
        case_id="case-read",
        kind="memory_read",
        payload={"episodic": ["memory-a"]},
    )
    _install(monkeypatch, _record(), [event])

    response = _influence()

    assert response["ok"] is True
    assert response["influences"] == []


def test_explicit_trace_decisions_are_reported(monkeypatch, inline_thread):
    at = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
    events = [
        TraceEvent(
            run_id="decision-run",
            case_id="case-a",
            kind="memory_attributed",
            timestamp=at,
            payload={
                "memory_ids": ["memory-a"],
                "items": [{"memory_id": "memory-a", "role": "episodic_hypothesis"}],
            },
        ),
        TraceEvent(
            run_id="decision-run",
            case_id="case-a",
            kind="memory_resolved",
            timestamp=at,
            payload={
                "memory_id": "memory-a",
                "fresh_probe_count": 2,
                "freshness_verified": True,
            },
        ),
        TraceEvent(
            run_id="shortcut-run",
            case_id="investigate:session-a",
            kind="memory_shortcut",
            timestamp=at,
            payload={
                "memory_ids": ["memory-a"],
                "skills": ["check_link", "check_route", "check_neighbour"],
                "candidate_probe_count": 10,
                "saved_probe_count": 7,
                "procedural_confidence": 1.5,
            },
        ),
    ]
    _install(monkeypatch, _record(), events)

    response = _influence()

    assert [item["kind"] for item in response["influences"]] == [
        "diagnosis_attribution",
        "diagnosis_resolution",
        "probe_shortcut",
    ]
    shortcut = response["influences"][-1]
    assert shortcut["what_changed"] == "探针集从 10 条收窄到 3 条"
    assert shortcut["evidence"]["procedural_confidence"] == 1.5


def test_prior_cycle_links_the_memory_to_the_escalation(monkeypatch, inline_thread):
    record = _record(with_cycle=True)
    _install(monkeypatch, record, [])
    escalation = {
        "kind": "escalated",
        "at": "2026-08-22T18:00:00+00:00",
        "detector": "failed_units",
        "subject": "demo-api.service",
        "action": "restart_unit",
        "recurrences": 3,
        "prior_cycles": [
            {"at": "2026-08-22T10:01:36+00:00", "outcome": "passed", "samples": 12},
            {"at": "2026-08-22T13:01:36+00:00", "outcome": "passed", "samples": 12},
        ],
    }
    monkeypatch.setattr(gateway, "_memory_influence_timeline", lambda: [escalation])

    response = _influence()

    influence = response["influences"][0]
    assert influence["kind"] == "escalation"
    assert influence["what_changed"] == "拒绝执行 restart_unit，转人工"
    assert influence["evidence"]["recurrences"] == 3
    assert influence["evidence"]["matching_prior_cycles"] == [escalation["prior_cycles"][0]]


def test_timeline_citation_carries_the_real_memory_record():
    record = _record(with_cycle=True)
    row = {
        "kind": "escalated",
        "detector": "failed_units",
        "subject": "demo-api.service",
        "action": "restart_unit",
        "prior_cycles": [{
            "at": "2026-08-22T10:01:36+00:00",
            "outcome": "passed",
            "samples": 12,
        }],
    }

    enriched = gateway._attach_prior_cycle_memories([row], [record])

    assert enriched[0]["prior_cycles"][0]["memory"] == {
        "memory_id": "memory-a",
        "tier": "episodic",
        "text": record.text,
    }
