"""The gateway separates live durable memory from the offline benchmark replay."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.evolve.observatory import CAPABILITIES
from core.memory.store import MemoryRecord, MemoryRelation
from frontend.gateway.app import main as gateway
from frontend.gateway.app import rca_reader


class _Repository:
    def __init__(self, records: list[MemoryRecord]):
        self.records = records
        self.reads = 0

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        assert include_quarantined is True
        self.reads += 1
        return list(self.records)


class _Store:
    def __init__(self, records: list[MemoryRecord], *, durable: bool = True):
        self.repository = _Repository(records) if durable else None
        self._records = records

    def records(self) -> list[MemoryRecord]:
        return list(self._records)


@pytest.fixture
def inline_thread(monkeypatch):
    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(gateway.asyncio, "to_thread", run_inline)


def _record(
    memory_id: str,
    tier: str,
    importance: float,
    *,
    quarantined: bool = False,
) -> MemoryRecord:
    observed = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    return MemoryRecord(
        memory_id=memory_id,
        tier=tier,
        text=f"{memory_id}: " + "x" * 300,
        tags=["network", "quarantine:superseded"] if quarantined else ["network"],
        asset_ids=["router-1"],
        confidence=1.5,
        importance=importance,
        strength=0.8,
        access_count=3,
        first_observed_at=observed,
        last_observed_at=observed,
        valid_from=observed,
        quarantined=quarantined,
        source_trace_ids=["trace-1", "trace-2"],
    )


def _list(monkeypatch, **kwargs):
    return asyncio.run(
        gateway.rca_memory(
            tier=kwargs.get("tier"),
            limit=kwargs.get("limit", 100),
            include_quarantined=kwargs.get("include_quarantined", False),
        )
    )


def test_missing_store_returns_a_non_durable_empty_snapshot(monkeypatch, inline_thread):
    monkeypatch.setattr(gateway, "_evolving_service", None)

    response = _list(monkeypatch)

    assert response["ok"] is True
    assert response["durable"] is False
    assert response["records"] == []
    assert response["counts"] == {
        "episodic": 0,
        "semantic": 0,
        "procedural": 0,
        "asset_profile": 0,
        "quarantined": 0,
    }
    assert response["budget"] == {"configured": None, "active": 0}


def test_list_reads_repository_and_applies_all_filters(monkeypatch, inline_thread):
    checkpoint = _record(
        "autopoiesis:memory-retention:decay-checkpoint",
        "semantic",
        1.0,
        quarantined=True,
    )
    checkpoint.tags = ["memory_retention_checkpoint"]
    records = [
        _record("episodic-low", "episodic", 1.0),
        _record("semantic-high", "semantic", 9.0),
        _record("episodic-high", "episodic", 5.0),
        _record("episodic-quarantined", "episodic", 20.0, quarantined=True),
        checkpoint,
    ]
    store = _Store(records)
    monkeypatch.setattr(
        gateway,
        "_evolving_service",
        SimpleNamespace(
            memory=store,
            health=lambda: {"memory_retention": {"budget": 2}},
        ),
    )

    default = _list(monkeypatch)
    filtered = _list(
        monkeypatch,
        tier="episodic",
        limit=2,
        include_quarantined=True,
    )

    assert default["durable"] is True
    assert [item["memory_id"] for item in default["records"]] == [
        "semantic-high",
        "episodic-high",
        "episodic-low",
    ]
    assert [item["memory_id"] for item in filtered["records"]] == [
        "episodic-quarantined",
        "episodic-high",
    ]
    assert default["counts"] == {
        "episodic": 2,
        "semantic": 1,
        "procedural": 0,
        "asset_profile": 0,
        "quarantined": 1,
    }
    assert default["budget"] == {"configured": 2, "active": 3}
    assert default["retention"]["decay_wired"] is CAPABILITIES["decay_wired"]
    assert default["retention"]["eviction_wired"] is CAPABILITIES["eviction_wired"]
    assert default["retention"]["last_decay_at"] == "2026-08-22T12:00:00+00:00"
    assert len(default["records"][0]["text"]) == 240
    assert default["records"][0]["source_trace_ids"] == 2
    assert store.repository.reads == 2


def test_detail_exposes_provenance_without_evidence_bodies(monkeypatch, inline_thread):
    record = _record("memory-audit", "episodic", 4.0, quarantined=True)
    record.links = ["memory-peer"]
    record.relations = [
        MemoryRelation(
            target_id="memory-peer",
            relation_type="precedes",
            evidence_ids=["evidence-1"],
        )
    ]
    record.evidence_snapshot = [
        {"evidence_id": "evidence-1", "summary": "secret body", "raw": {"token": "secret"}},
        {"evidence_id": "evidence-2", "payload": "also secret"},
    ]
    store = _Store([record])
    monkeypatch.setattr(gateway, "_evolving_service", SimpleNamespace(memory=store))

    response = asyncio.run(gateway.rca_memory_detail("memory-audit"))
    detail = response["record"]

    assert detail["text"] == record.text
    assert detail["source_trace_ids"] == ["trace-1", "trace-2"]
    assert detail["links"] == ["memory-peer"]
    assert detail["relations"][0]["target_id"] == "memory-peer"
    assert detail["quarantine_reason"] == "superseded"
    assert detail["evidence_snapshot"] == {
        "count": 2,
        "evidence_ids": ["evidence-1", "evidence-2"],
    }
    assert "secret" not in str(detail["evidence_snapshot"])


def test_unknown_memory_id_returns_404(monkeypatch, inline_thread):
    monkeypatch.setattr(
        gateway,
        "_evolving_service",
        SimpleNamespace(memory=_Store([], durable=False)),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(gateway.rca_memory_detail("missing"))

    assert exc_info.value.status_code == 404


def test_evolution_labels_the_offline_replay(monkeypatch, inline_thread):
    monkeypatch.setattr(
        rca_reader,
        "load_evolution",
        lambda _manifest, passes: {
            "ready": True,
            "passes": passes,
            "nCases": 7,
            "cases": [{}] * 7,
        },
    )

    response = asyncio.run(gateway.rca_evolution(passes=3))

    assert response["dataMode"] == "offline_benchmark_replay"
    assert response["onlineMemory"] is False
    assert response["benchmark"] == {"caseCount": 7, "passes": 3}
