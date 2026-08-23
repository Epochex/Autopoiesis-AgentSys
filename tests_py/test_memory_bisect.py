"""Counterfactual memory probes locate contamination without changing the store."""
from __future__ import annotations

import json

import pytest

from core.memory.bisect import bisect
from core.memory.store import MemoryRecord, TieredMemoryStore


def _record(memory_id: str, access_count: int) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tier="episodic",
        text=f"memory {memory_id}",
        tags=[memory_id],
        access_count=access_count,
    )


def _store() -> TieredMemoryStore:
    store = TieredMemoryStore()
    store.seed(
        [
            _record("frequent", 12),
            _record("culprit", 8),
            _record("rare", 1),
        ]
    )
    return store


def _snapshot(store: TieredMemoryStore) -> list[dict]:
    return [record.model_dump(mode="json") for record in store.records()]


def test_single_culprit_is_found_in_access_count_order_and_store_is_restored():
    store = _store()
    before = _snapshot(store)
    before_index = store.index_health()

    def reproduce() -> bool:
        culprit = store.get("culprit")
        return culprit is not None and not culprit.quarantined

    result = bisect(store, reproduce)

    assert result.culprit_id == "culprit"
    assert result.probe_count == 2
    assert result.probes == [("frequent", True), ("culprit", False)]
    assert _snapshot(store) == before
    assert store.index_health() == before_index
    assert json.loads(result.model_dump_json()) == {
        "culprit_id": "culprit",
        "probe_count": 2,
        "probes": [["frequent", True], ["culprit", False]],
    }


def test_no_culprit_returns_none_without_changing_the_store():
    store = _store()
    before = _snapshot(store)

    result = bisect(store, lambda: True)

    assert result.culprit_id is None
    assert result.probe_count == 3
    assert result.probes == [
        ("frequent", True),
        ("culprit", True),
        ("rare", True),
    ]
    assert _snapshot(store) == before


def test_original_quarantine_state_is_preserved():
    store = _store()
    store.quarantine("rare", "known_bad")
    before = _snapshot(store)

    result = bisect(store, lambda: True)

    assert [memory_id for memory_id, _ in result.probes] == ["frequent", "culprit"]
    assert _snapshot(store) == before


def test_store_is_restored_when_reproducer_raises():
    store = _store()
    before = _snapshot(store)
    before_index = store.index_health()

    def reproduce() -> bool:
        assert store.get("frequent").quarantined
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError, match="probe failed"):
        bisect(store, reproduce)

    assert _snapshot(store) == before
    assert store.index_health() == before_index


def test_max_probes_returns_unlocated_instead_of_guessing():
    store = _store()
    before = _snapshot(store)

    def reproduce() -> bool:
        culprit = store.get("rare")
        return culprit is not None and not culprit.quarantined

    result = bisect(store, reproduce, max_probes=2)

    assert result.culprit_id is None
    assert result.probe_count == 2
    assert result.probes == [("frequent", True), ("culprit", True)]
    assert _snapshot(store) == before
