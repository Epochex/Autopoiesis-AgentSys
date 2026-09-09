from __future__ import annotations

from types import SimpleNamespace

from core.memory.quality import apply_memory_quality, assess_memory
from core.memory.store import MemoryClaim, MemoryRecord, TieredMemoryStore


def test_atomic_claims_block_shallow_generated_memory_from_retrieval() -> None:
    record = MemoryRecord(
        memory_id="shallow",
        tier="procedural",
        text="follow the template",
        source_trace_ids=["run-1"],
        claims=[MemoryClaim(kind="trigger", statement="network issue")],
    )

    quality = apply_memory_quality(record)
    store = TieredMemoryStore()
    store.add(record)

    assert quality.retrieval_ready is False
    assert "missing_claim:probe" in quality.issues
    assert store.retrieve(["network", "issue"], [])["procedural"] == []
    assert store.index_health()["quality_blocked_records"] == 1


def test_evidence_linked_procedure_is_indexed_and_recalled() -> None:
    record = MemoryRecord(
        memory_id="useful",
        tier="procedural",
        text="verify routing after a topology change",
        source_trace_ids=["run-1"],
        claims=[
            MemoryClaim(kind="trigger", statement="Routing changed after topology update"),
            MemoryClaim(
                kind="probe",
                statement="Read the current route and interface state",
                evidence_ids=["ev-route"],
            ),
            MemoryClaim(
                kind="expected_signal",
                statement="The active route must use an interface with carrier",
                evidence_ids=["ev-route"],
            ),
        ],
    )

    quality = apply_memory_quality(record)
    store = TieredMemoryStore()
    store.add(record)

    assert quality.score == 1.0
    assert store.retrieve(["routing", "interface"], [])["procedural"] == [record]


def test_legacy_hand_authored_memory_keeps_backward_compatible_default() -> None:
    record = MemoryRecord(memory_id="legacy", tier="semantic", text="known route fact")

    assert assess_memory(record).retrieval_ready is False
    assert record.retrieval_ready is True


class _RecordingVector:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def upsert(self, *_args, **_kwargs):
        return True

    def delete(self, *_args, **_kwargs):
        return True

    def search(self, query: str, k: int = 10):
        self.queries.append(query)
        return [SimpleNamespace(memory_id="route", score=0.8)]

    def compact(self):
        return 0

    def should_compact(self):
        return False

    def health(self):
        return {"ok": True}


def test_dense_route_uses_query_shape_instead_of_text_length() -> None:
    vector = _RecordingVector()
    store = TieredMemoryStore(vector_index=vector)
    store.add(MemoryRecord(
        memory_id="route",
        tier="episodic",
        text="192.168.1.20 route changed after failover",
        asset_ids=["192.168.1.20"],
    ))

    store.retrieve(["192.168.1.20"], ["192.168.1.20"])
    assert vector.queries == []

    store.retrieve(["切换之后访问路径发生变化"], [])
    assert vector.queries == ["切换之后访问路径发生变化"]
