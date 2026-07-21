from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.memory.index_projector import IndexProjectionError, MemoryIndexProjector
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.orchestrator.evolving_service import EvolvingRCAService
from domains.network_rca import build_network_rca_service
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases


def _event(offset: int, memory_id: str, text: str, *, quarantined: bool = False):
    return SimpleNamespace(
        event_offset=offset,
        memory_id=memory_id,
        version=2 if quarantined else 1,
        event_type="QUARANTINE" if quarantined else "UPSERT",
        record=MemoryRecord(
            memory_id=memory_id,
            tier="semantic",
            text=text,
            tags=text.split(),
            quarantined=quarantined,
        ),
        occurred_at=datetime.now(timezone.utc),
    )


class _EventRepository:
    def __init__(self, events):
        self.events = list(events)
        self.checkpoint = 0
        self.fail_checkpoint_once_at: int | None = None

    def get_checkpoint(self, _index_name: str) -> int:
        return self.checkpoint

    def read_events(self, *, after_offset: int = 0, limit: int = 1_000):
        return [event for event in self.events if event.event_offset > after_offset][:limit]

    def advance_checkpoint(self, _index_name: str, event_offset: int) -> int:
        if self.fail_checkpoint_once_at == event_offset:
            self.fail_checkpoint_once_at = None
            raise RuntimeError("checkpoint unavailable")
        self.checkpoint = event_offset
        return self.checkpoint


class _VersionedEventRepository(_EventRepository):
    def __init__(self, events, current: MemoryRecord, version: int):
        super().__init__(events)
        self.current = current
        self.version = version

    def load_versioned_records(self, *, include_quarantined: bool = True):
        return [(self.current.model_copy(deep=True), self.version)]

    def sync_records(self, records, *, expected_versions=None):
        raise AssertionError("projection must not flush the source snapshot")


class _VectorProjection:
    def __init__(self):
        self.documents: dict[str, str] = {}
        self.fail_id: str | None = None

    def upsert(self, memory_id: str, text: str, **_kwargs) -> bool:
        if memory_id == self.fail_id:
            raise RuntimeError("embedding failed")
        self.documents[memory_id] = text
        return True

    def delete(self, memory_id: str, **_kwargs) -> bool:
        self.documents.pop(memory_id, None)
        return True

    def search(self, _query: str, k: int = 10):
        return []

    def compact(self) -> int:
        return 1

    def should_compact(self) -> bool:
        return False

    def health(self):
        return {"healthy": True, "compaction_due": False}


def test_projection_stops_at_failed_event_and_resumes_without_checkpoint_gap():
    repository = _EventRepository(
        [
            _event(1, "m1", "packet loss"),
            _event(2, "m2", "latency spike"),
            _event(3, "m1", "retired", quarantined=True),
        ]
    )
    vector = _VectorProjection()
    vector.fail_id = "m2"
    memory = TieredMemoryStore(vector_index=vector)
    projector = MemoryIndexProjector(repository, memory, batch_size=10)

    with pytest.raises(IndexProjectionError, match="event_offset=2"):
        projector.sync_once()
    assert repository.checkpoint == 1
    assert memory.projected_offset == 1
    assert memory.get("m1") is not None
    assert memory.get("m2") is None

    vector.fail_id = None
    assert projector.sync_pending() == 2
    assert repository.checkpoint == 3
    assert memory.get("m1").quarantined  # type: ignore[union-attr]
    assert memory.get("m2").text == "latency spike"  # type: ignore[union-attr]
    assert "m1" not in vector.documents
    assert projector.stats()["events_applied"] == 3


def test_projection_replays_when_index_write_succeeds_before_checkpoint_failure():
    repository = _EventRepository([_event(1, "m1", "link flap")])
    repository.fail_checkpoint_once_at = 1
    memory = TieredMemoryStore()
    projector = MemoryIndexProjector(repository, memory)

    with pytest.raises(IndexProjectionError, match="checkpoint unavailable"):
        projector.sync_once()
    assert memory.projected_offset == 1
    assert repository.checkpoint == 0

    assert projector.sync_once() == 1
    assert repository.checkpoint == 1
    assert len(memory.records()) == 1
    assert projector.stats()["replayed"] == 1


def test_snapshot_newer_than_checkpoint_is_not_regressed_by_old_events():
    old = _event(1, "m1", "old state")
    current_event = _event(2, "m1", "current state")
    current_event.version = 2
    current = current_event.record.model_copy(deep=True)
    repository = _VersionedEventRepository([old, current_event], current, version=2)
    memory = TieredMemoryStore.from_repository(repository)
    projector = MemoryIndexProjector(repository, memory)

    assert projector.sync_pending() == 2
    assert memory.get("m1").text == "current state"  # type: ignore[union-attr]
    assert projector.stats()["events_applied"] == 0
    assert projector.stats()["replayed"] == 2


class _Worker:
    def __init__(self):
        self.started = 0
        self.triggered = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def trigger(self) -> None:
        self.triggered += 1

    def stop(self, timeout: float = 5.0) -> bool:
        self.stopped += 1
        return True

    def stats(self):
        return {"started": self.started, "triggered": self.triggered}


def test_evolving_service_learns_only_after_verification_and_triggers_maintenance(tmp_path):
    orchestrator = build_network_rca_orchestrator(
        tmp_path / "trace.jsonl",
        seed_memory=False,
    )
    workers = [_Worker(), _Worker()]
    service = EvolvingRCAService(orchestrator, maintenance_workers=workers)

    diagnosis, verification = service.diagnose(load_seed_cases()[0])

    assert diagnosis.root_cause_key
    assert verification.passed
    assert service.last_consolidation is not None
    assert service.last_consolidation.passed
    assert service.memory.active()
    assert [worker.triggered for worker in workers] == [1, 1]
    assert service.health()["consolidations"] == 1
    assert service.close()
    assert [worker.stopped for worker in workers] == [1, 1]
    with pytest.raises(RuntimeError, match="closed"):
        service.diagnose(load_seed_cases()[0])


def test_evolving_service_does_not_consolidate_rejected_diagnosis():
    class _RejectedRuntime:
        def __init__(self):
            self.memory = TieredMemoryStore()
            self.skills = SimpleNamespace(all=lambda: [])
            self.last_run_id = "run-rejected"
            self._run_events = []
            self._last_evidence = []

        def diagnose(self, _case):
            return SimpleNamespace(root_cause_key="unknown"), SimpleNamespace(passed=False)

    workers = [_Worker()]
    service = EvolvingRCAService(_RejectedRuntime(), maintenance_workers=workers)
    diagnosis, verification = service.diagnose(SimpleNamespace(id="case-1"))

    assert diagnosis.root_cause_key == "unknown"
    assert not verification.passed
    assert service.last_consolidation is None
    assert service.health()["consolidations"] == 0
    assert workers[0].triggered == 0
    service.close()


def test_failed_durable_consolidation_restores_local_memory_snapshot(tmp_path):
    class _FailingRepository:
        def load_versioned_records(self, *, include_quarantined=True):
            assert include_quarantined
            return []

        def sync_records(self, records, *, expected_versions=None):
            del records, expected_versions
            raise RuntimeError("simulated CAS conflict")

    orchestrator = build_network_rca_orchestrator(
        tmp_path / "rollback-trace.jsonl", seed_memory=False
    )
    orchestrator.memory = TieredMemoryStore.from_repository(_FailingRepository())
    service = EvolvingRCAService(
        orchestrator,
        maintenance_workers=[_Worker()],
        raise_on_evolution_error=False,
    )

    diagnosis, verification = service.diagnose(load_seed_cases()[0])

    assert diagnosis.root_cause_key
    assert verification.passed
    assert service.memory.records() == []
    assert service.health()["consolidation_failures"] == 1
    assert "simulated CAS conflict" in service.health()["last_error"]
    service.close()


def test_evolving_service_refuses_to_reason_while_projection_is_still_lagging():
    class _Runtime:
        def __init__(self, memory):
            self.memory = memory
            self.skills = SimpleNamespace(all=lambda: [])
            self.called = False

        def diagnose(self, _case):
            self.called = True
            raise AssertionError("stale memory must not reach the reasoner")

    repository = _EventRepository(
        [_event(1, "m1", "one"), _event(2, "m2", "two")]
    )
    memory = TieredMemoryStore()
    projector = MemoryIndexProjector(repository, memory, batch_size=1)
    runtime = _Runtime(memory)
    service = EvolvingRCAService(
        runtime,
        projector=projector,
        maintenance_workers=[_Worker()],
        projection_max_batches=1,
    )

    with pytest.raises(RuntimeError, match="remains behind"):
        service.diagnose(SimpleNamespace(id="case-1"))
    assert not runtime.called
    assert repository.checkpoint == 1
    service.close()


def test_network_service_factory_exports_the_closed_loop_runtime(tmp_path):
    service = build_network_rca_service(
        tmp_path / "service-trace.jsonl",
        seed_memory=False,
        start_maintenance=False,
    )
    assert isinstance(service, EvolvingRCAService)
    assert service.projector is None
    assert service.close()
