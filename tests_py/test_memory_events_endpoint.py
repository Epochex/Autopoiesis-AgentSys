"""The live-memory event endpoint pages the durable append-only ledger."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.memory.store import MemoryRecord
from frontend.gateway.app import main as gateway


class _Repository:
    def __init__(self, events: list[SimpleNamespace]):
        self.events = events
        self.reads: list[tuple[int, int]] = []

    def read_events(self, *, after_offset: int, limit: int) -> list[SimpleNamespace]:
        self.reads.append((after_offset, limit))
        return [event for event in self.events if event.event_offset > after_offset][:limit]


@pytest.fixture
def inline_thread(monkeypatch):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(gateway.asyncio, "to_thread", run_inline)


def _event(
    offset: int,
    *,
    event_type: str = "UPSERT",
    quarantined: bool = False,
) -> SimpleNamespace:
    occurred_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=offset)
    record = MemoryRecord(
        memory_id=f"memory-{offset}",
        tier="episodic",
        text="事件正文" * 80,
        tags=["network", "quarantine:baseline-contamination"] if quarantined else ["network"],
        confidence=1.5,
        importance=4.0,
        strength=0.8,
        first_observed_at=occurred_at,
        last_observed_at=occurred_at,
        valid_from=occurred_at,
        quarantined=quarantined,
    )
    return SimpleNamespace(
        event_offset=offset,
        memory_id=record.memory_id,
        version=offset,
        event_type=event_type,
        record=record,
        occurred_at=occurred_at,
    )


def _install(monkeypatch, events: list[SimpleNamespace]) -> _Repository:
    repository = _Repository(events)
    memory = SimpleNamespace(repository=repository)
    monkeypatch.setattr(gateway, "_evolving_service", SimpleNamespace(memory=memory))
    return repository


def _read(*, after: int = 0, limit: int = 500):
    return asyncio.run(gateway.rca_memory_events(after=after, limit=limit))


def test_empty_durable_ledger_returns_an_empty_page(monkeypatch, inline_thread):
    repository = _install(monkeypatch, [])

    response = _read()

    assert response == {
        "ok": True,
        "durable": True,
        "total": 0,
        "next_offset": None,
        "high_water": 0,
        "events": [],
    }
    assert repository.reads == [(0, 500)]


def test_pages_by_exclusive_event_offset(monkeypatch, inline_thread):
    repository = _install(monkeypatch, [_event(1), _event(2), _event(3)])

    first = _read(limit=2)
    second = _read(after=first["next_offset"], limit=2)

    assert [event["offset"] for event in first["events"]] == [1, 2]
    assert first["total"] == 2
    assert first["next_offset"] == 2
    assert first["high_water"] == 2
    assert [event["offset"] for event in second["events"]] == [3]
    assert second["total"] == 1
    assert second["next_offset"] is None
    assert second["high_water"] == 3
    assert repository.reads == [(0, 2), (2, 2)]


def test_missing_durable_repository_is_a_successful_fallback(monkeypatch, inline_thread):
    monkeypatch.setattr(
        gateway,
        "_evolving_service",
        SimpleNamespace(memory=SimpleNamespace(repository=None)),
    )

    response = _read()

    assert response == {
        "ok": True,
        "durable": False,
        "events": [],
        "total": 0,
        "high_water": 0,
    }


def test_high_water_advances_past_hidden_administrative_events(monkeypatch, inline_thread):
    _install(monkeypatch, [_event(7, event_type="DELETE")])

    response = _read(after=3)

    assert response["events"] == []
    assert response["next_offset"] is None
    assert response["high_water"] == 7


def test_quarantine_reason_and_bounded_text_come_from_event_version(monkeypatch, inline_thread):
    event = _event(34, event_type="QUARANTINE", quarantined=True)
    _install(monkeypatch, [event])

    response = _read()
    item = response["events"][0]

    assert item["event_type"] == "QUARANTINE"
    assert item["quarantine_reason"] == "baseline-contamination"
    assert item["occurred_at"] == event.occurred_at.isoformat()
    assert item["strength"] == 0.8
    assert len(item["text_head"]) == 120
    assert event.record.text.startswith(item["text_head"])
