"""TieredMemoryStore contract — the substrate every memory claim rests on:
unique ids, tier-partitioned deterministic retrieval, quarantine isolation."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from typing import Sequence

from core.evolve.memory_ops import decay_and_forget
from core.memory.postgres_repository import MemoryVersionConflict, MemoryWrite
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


class _VersionedFakeRepository:
    """Small transactional double for the store's optimistic write contract."""

    def __init__(self, records: Sequence[MemoryRecord]):
        self.state = {
            record.memory_id: record.model_copy(deep=True) for record in records
        }
        self.versions = {record.memory_id: 1 for record in records}
        self.events: list[tuple[str, int]] = []
        self.synced_batches: list[list[str]] = []

    def load_versioned_records(
        self, *, include_quarantined: bool = True
    ) -> list[tuple[MemoryRecord, int]]:
        assert include_quarantined
        return [
            (self.state[memory_id].model_copy(deep=True), self.versions[memory_id])
            for memory_id in sorted(self.state)
        ]

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        return [
            record for record, _version in self.load_versioned_records(
                include_quarantined=include_quarantined
            )
        ]

    def sync_records(self, records, *, expected_versions=None):
        assert expected_versions is not None
        # Preflight mirrors a database transaction: no state or event mutation
        # is visible if any expected version is stale.
        for record in records:
            actual = self.versions.get(record.memory_id, 0)
            expected = expected_versions[record.memory_id]
            if actual != expected:
                raise MemoryVersionConflict(
                    f"memory {record.memory_id!r} expected version {expected}, "
                    f"current version is {actual}"
                )
        writes = []
        self.synced_batches.append([record.memory_id for record in records])
        for record in records:
            version = self.versions.get(record.memory_id, 0) + 1
            self.state[record.memory_id] = record.model_copy(deep=True)
            self.versions[record.memory_id] = version
            self.events.append((record.memory_id, version))
            writes.append(MemoryWrite(record.memory_id, version, len(self.events), "UPSERT"))
        return writes


class _FakeVectorIndex:
    def __init__(self):
        self.documents: dict[str, str] = {}
        self.deleted: list[str] = []

    def upsert(self, memory_id: str, text: str, **_kwargs) -> bool:
        self.documents[memory_id] = text
        return True

    def delete(self, memory_id: str, **_kwargs) -> bool:
        self.documents.pop(memory_id, None)
        self.deleted.append(memory_id)
        return True

    def search(self, query: str, k: int = 10):
        del query, k
        return [SimpleNamespace(memory_id=mid, score=0.9, version=1) for mid in self.documents]

    def compact(self) -> int:
        return len(self.documents)

    def should_compact(self) -> bool:
        return False

    def health(self) -> dict:
        return {"live_documents": len(self.documents), "base_index_type": "hnsw"}


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


def test_versioned_store_flush_uses_dirty_record_cas_without_false_conflicts():
    repository = _VersionedFakeRepository([_rec("m1"), _rec("m2")])
    first = TieredMemoryStore.from_repository(repository)
    second = TieredMemoryStore.from_repository(repository)

    first.get("m1").text = "first writer"  # type: ignore[union-attr]
    second.get("m2").text = "independent writer"  # type: ignore[union-attr]
    assert [write.version for write in first.flush()] == [2]
    assert [write.version for write in second.flush()] == [2]
    assert repository.synced_batches == [["m1"], ["m2"]]
    assert first.flush() == []

    winner = TieredMemoryStore.from_repository(repository)
    stale = TieredMemoryStore.from_repository(repository)
    winner.get("m1").text = "winner"  # type: ignore[union-attr]
    stale.get("m1").text = "stale"  # type: ignore[union-attr]
    winner.flush()
    event_count = len(repository.events)
    with pytest.raises(MemoryVersionConflict):
        stale.flush()
    assert len(repository.events) == event_count
    assert repository.state["m1"].text == "winner"


def test_bounded_graph_expansion_retrieves_related_cross_event_memories():
    store = TieredMemoryStore()
    seed = MemoryRecord(
        memory_id="seed",
        tier="semantic",
        text="payment gateway timeout",
        links=["bridge"],
    )
    bridge = MemoryRecord(
        memory_id="bridge",
        tier="episodic",
        text="certificate rotation preceded intermittent failures",
        links=["seed", "latent"],
    )
    latent = MemoryRecord(
        memory_id="latent",
        tier="procedural",
        text="compare trust-store generations across dependent services",
        links=["bridge"],
    )
    store.seed([seed, bridge, latent])

    without_graph = store.retrieve(["payment", "gateway"], [], limit_per_tier=5)
    assert without_graph["semantic"] == [seed]
    assert without_graph["episodic"] == []

    with_graph = store.retrieve(
        ["payment", "gateway"], [], limit_per_tier=5, graph_depth=2
    )
    assert with_graph["semantic"] == [seed]
    assert with_graph["episodic"] == [bridge]
    assert with_graph["procedural"] == [latent]
    diagnostics = {item["memory_id"]: item for item in store.retrieval_diagnostics()}
    assert diagnostics["seed"]["graph_hop"] == 0
    assert diagnostics["bridge"]["graph_hop"] == 1
    assert diagnostics["bridge"]["graph_parent_id"] == "seed"
    assert diagnostics["latent"]["graph_hop"] == 2

    store.quarantine("bridge", "contradicted")
    isolated = store.retrieve(
        ["payment", "gateway"], [], limit_per_tier=5, graph_depth=2
    )
    assert isolated["episodic"] == []
    assert isolated["procedural"] == []


def test_online_dense_route_adds_semantic_candidate_and_tracks_mutations():
    vector = _FakeVectorIndex()
    store = TieredMemoryStore()
    record = MemoryRecord(
        memory_id="dense-only",
        tier="semantic",
        text="upstream identity service rejected an expired certificate",
        tags=["certificate"],
    )
    store.add(record)

    # Build/restore produces one immutable generation, then the store attaches
    # the projection. Subsequent mutations are applied to its exact delta.
    vector.documents[record.memory_id] = store.vector_document(record)
    store.attach_vector_index(vector)
    result = store.retrieve(["authentication", "credential"], [], limit_per_tier=3)
    assert result["semantic"] == [record]
    assert store.index_health()["vector"]["base_index_type"] == "hnsw"

    record.tags.append("rotation")
    store.reindex(record.memory_id)
    assert "rotation" in vector.documents[record.memory_id]

    store.quarantine(record.memory_id, "contradicted")
    assert record.memory_id in vector.deleted
    assert store.retrieve(["authentication"], [], limit_per_tier=3)["semantic"] == []


def test_dense_dependency_degradation_is_visible_and_sparse_route_stays_live():
    store = TieredMemoryStore()
    record = _rec("sparse", tags=["carrier"])
    store.add(record)
    store.mark_vector_degraded("faiss unavailable")

    assert store.retrieve(["carrier"], [])["episodic"] == [record]
    health = store.index_health()
    assert health["vector_enabled"] is False
    assert health["vector_degraded"] is True
    assert health["vector_degraded_reason"] == "faiss unavailable"


def test_decay_rejects_nonsensical_parameters():
    store = TieredMemoryStore()
    with pytest.raises(ValueError, match="retention"):
        decay_and_forget(store, retention=0.0)
    with pytest.raises(ValueError, match="retention"):
        decay_and_forget(store, retention=1.5)
    with pytest.raises(ValueError, match="floor"):
        decay_and_forget(store, floor=-0.1)
