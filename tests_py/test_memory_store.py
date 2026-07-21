"""TieredMemoryStore contract — the substrate every memory claim rests on:
unique ids, tier-partitioned deterministic retrieval, quarantine isolation."""
from __future__ import annotations

import pytest
from typing import Sequence

from core.evolve.memory_ops import decay_and_forget
from core.memory.store import MemoryRecord, TieredMemoryStore


def _rec(mid: str, *, tier: str = "episodic", tags: list[str] | None = None, confidence: float = 1.0) -> MemoryRecord:
    return MemoryRecord(memory_id=mid, tier=tier, text=f"{mid} text", tags=tags or ["carrier"], confidence=confidence)


class _FakeRepository:
    def __init__(self, records: Sequence[MemoryRecord]):
        self.loaded = [record.model_copy(deep=True) for record in records]
        self.synced: list[MemoryRecord] = []

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        assert include_quarantined
        return [record.model_copy(deep=True) for record in self.loaded]

    def sync_records(self, records: Sequence[MemoryRecord]) -> list[str]:
        self.synced = [record.model_copy(deep=True) for record in records]
        return [record.memory_id for record in records]


def test_duplicate_memory_id_fails_loud_on_add_and_seed():
    store = TieredMemoryStore()
    store.add(_rec("m1"))
    with pytest.raises(ValueError, match="duplicate memory_id"):
        store.add(_rec("m1"))
    with pytest.raises(ValueError, match="duplicate memory_id"):
        store.seed([_rec("m2"), _rec("m2")])


def test_disabled_store_and_non_positive_limit_return_empty_tiers():
    empty = {"episodic": [], "semantic": [], "procedural": [], "asset_profile": []}
    disabled = TieredMemoryStore(enabled=False)
    disabled.add(_rec("m1"))
    assert disabled.retrieve(["carrier"], []) == empty

    store = TieredMemoryStore()
    store.add(_rec("m1"))
    assert store.retrieve(["carrier"], [], limit_per_tier=0) == empty


def test_retrieval_requires_overlap_and_breaks_score_ties_by_insertion_order():
    store = TieredMemoryStore()
    store.add(_rec("first", tags=["carrier"]))
    store.add(_rec("second", tags=["carrier"]))          # identical score → older first
    store.add(_rec("unrelated", tags=["fortiguard"]))

    got = store.retrieve(["carrier"], [], limit_per_tier=5)["episodic"]
    assert [r.memory_id for r in got] == ["first", "second"]   # no-overlap record absent

    assert store.retrieve([], []) == {
        "episodic": [], "semantic": [], "procedural": [], "asset_profile": [],
    }


def test_quarantined_records_are_hidden_from_retrieval_but_kept_for_audit():
    store = TieredMemoryStore()
    store.add(_rec("m1"))
    store.quarantine("m1", "contradicted")
    assert store.retrieve(["carrier"], [])["episodic"] == []
    assert store.active() == []
    kept = store.records()
    assert len(kept) == 1 and kept[0].quarantined
    assert "quarantine:contradicted" in kept[0].tags
    store.quarantine("missing-id", "noop")               # unknown id is a no-op


def test_incremental_index_tracks_add_reindex_delete_and_compaction():
    store = TieredMemoryStore(
        lexical_seal_threshold=1,
        lexical_compact_segment_threshold=20,
    )
    record = _rec("m1", tags=["carrier"])
    store.add(record)
    assert store.retrieve(["carrier"], [])["episodic"] == [record]

    record.tags.append("payment")
    assert store.reindex(record.memory_id)
    assert store.retrieve(["payment"], [])["episodic"] == [record]
    before = store.index_health()
    assert before["physical_entries"] == 2
    assert before["obsolete_entries"] == 1

    store.quarantine(record.memory_id, "superseded")
    assert store.retrieve(["payment"], [])["episodic"] == []
    assert store.index_health()["obsolete_entries"] == 3
    assert store.compact_index(force=True)
    assert store.index_health()["physical_entries"] == 0


def test_incremental_asset_lookup_tracks_asset_changes():
    store = TieredMemoryStore()
    record = MemoryRecord(
        memory_id="asset-move",
        tier="episodic",
        text="unrelated",
        asset_ids=["old"],
    )
    store.add(record)
    assert store.retrieve([], ["old"])["episodic"] == [record]

    record.asset_ids[:] = ["new"]
    store.reindex(record.memory_id)
    assert store.retrieve([], ["old"])["episodic"] == []
    assert store.retrieve([], ["new"])["episodic"] == [record]


def test_repository_restart_rebuilds_derived_index_and_flushes_complete_state():
    active = _rec("persisted", tags=["支付链路"])
    quarantined = _rec("audit-only", tags=["支付链路"])
    quarantined.quarantined = True
    repository = _FakeRepository([active, quarantined])

    restarted = TieredMemoryStore.from_repository(repository)

    assert [record.memory_id for record in restarted.records()] == ["persisted", "audit-only"]
    assert restarted.retrieve(["支付链路"], [], limit_per_tier=5)["episodic"] == [active]
    assert restarted.flush() == ["persisted", "audit-only"]
    assert repository.synced == restarted.records()


def test_decay_rejects_nonsensical_parameters():
    store = TieredMemoryStore()
    with pytest.raises(ValueError, match="retention"):
        decay_and_forget(store, retention=0.0)
    with pytest.raises(ValueError, match="retention"):
        decay_and_forget(store, retention=1.5)
    with pytest.raises(ValueError, match="floor"):
        decay_and_forget(store, floor=-0.1)
