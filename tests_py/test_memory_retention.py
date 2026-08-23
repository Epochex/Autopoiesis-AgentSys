from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import core.evolve.consolidate as consolidate_module
import core.orchestrator.evolving_service as service_module
from core.evolve.consolidate import ConsolidationReport
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.orchestrator.evolving_service import (
    EvolvingRCAService,
    memory_retention_wiring,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
CASE = SimpleNamespace(id="case-retention", query="diagnose", assets=[])


class _SnapshotRepository:
    def __init__(self, records: list[MemoryRecord]) -> None:
        self.stored = [record.model_copy(deep=True) for record in records]
        self.attempted: list[list[MemoryRecord]] = []
        self.fail_writes = False

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        assert include_quarantined
        return [record.model_copy(deep=True) for record in self.stored]

    def sync_records(self, records, *, expected_versions=None):
        assert expected_versions is None
        snapshot = [record.model_copy(deep=True) for record in records]
        self.attempted.append(snapshot)
        if self.fail_writes:
            raise RuntimeError("retention transaction failed")
        self.stored = snapshot
        return []


class _VerifiedOrchestrator:
    def __init__(self, memory: TieredMemoryStore) -> None:
        self.memory = memory
        self.skills = SimpleNamespace(all=lambda: [])
        self.last_run_id = "verified-run"
        self._run_events = []
        self._last_evidence = []

    def diagnose(self, _case):
        return (
            SimpleNamespace(root_cause_key="retention-test"),
            SimpleNamespace(passed=True),
        )


@pytest.fixture(autouse=True)
def _controlled_retention_environment(monkeypatch):
    monkeypatch.delenv("AUTOPOIESIS_MEMORY_BUDGET", raising=False)
    monkeypatch.delenv("AUTOPOIESIS_MEMORY_DECAY_INTERVAL", raising=False)
    monkeypatch.setattr(service_module, "_utc_now", lambda: NOW)


@pytest.fixture
def no_op_consolidation(monkeypatch):
    def consolidate(events, case, memory, skills, evidence, **_options):
        del events, skills, evidence
        memory.flush()
        return ConsolidationReport(run_id=case.id, passed=True)

    monkeypatch.setattr(consolidate_module, "consolidate_run", consolidate)


def _record(
    memory_id: str,
    *,
    tags: list[str] | None = None,
    strength: float = 1.0,
    importance: float = 1.0,
    access_count: int = 0,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tier="semantic",
        text=memory_id,
        tags=list(tags or []),
        strength=strength,
        importance=importance,
        access_count=access_count,
    )


def _checkpoint(at: datetime) -> MemoryRecord:
    return MemoryRecord(
        memory_id=service_module._DECAY_CHECKPOINT_ID,
        tier="semantic",
        text="Persistent timestamp for the memory decay schedule.",
        tags=["seed", service_module._DECAY_CHECKPOINT_TAG],
        quarantined=True,
        last_observed_at=at,
        event_type=service_module._DECAY_CHECKPOINT_TAG,
    )


def _service(repository: _SnapshotRepository, **options) -> EvolvingRCAService:
    memory = TieredMemoryStore.from_repository(repository)
    return EvolvingRCAService(
        _VerifiedOrchestrator(memory),
        maintenance_workers=[],
        start_maintenance=False,
        **options,
    )


def _payloads(records: list[MemoryRecord]) -> list[dict]:
    return [record.model_dump(mode="json") for record in records]


def test_unconfigured_budget_preserves_existing_active_memory(no_op_consolidation):
    records = [_record("one"), _record("two"), _checkpoint(NOW)]
    repository = _SnapshotRepository(records)
    service = _service(repository)
    before = _payloads(service.memory.active())

    service.diagnose(CASE)

    assert _payloads(service.memory.active()) == before
    # The flat pair is kept for compatibility, but `capabilities` is the honest
    # answer: a mechanism can be wired into the production path and still not
    # fire on this instance. Eviction is exactly that case without a budget —
    # collapsing the two into one boolean is how the old flags came to lie.
    retention = service.health()["memory_retention"]
    assert retention["capabilities"]["decay"] == {
        "implemented": True, "production_wired": True, "configured": True,
    }
    assert retention["capabilities"]["eviction"] == {
        "implemented": True, "production_wired": True, "configured": False,
    }, "no budget means eviction cannot fire, however well it is wired"
    # The flat decay_wired/eviction_wired pair is gone from health() on purpose:
    # one boolean could not separate "the call site exists" from "it fires here",
    # and that conflation is exactly how the old flag came to claim True while
    # eviction was unreachable.
    assert {k: v for k, v in retention.items() if k != "capabilities"} == {
        "budget": None,
        "decay_interval_seconds": 86400.0,
    }
    # The function returns the structured form, not the flat pair — that change
    # is the point: a hardcoded {"decay_wired": True} could not tell a wired
    # mechanism from a firing one, and said True even where the call site had
    # been deleted.
    assert memory_retention_wiring() == {
        "decay": {"implemented": True, "production_wired": True, "configured": True},
        "eviction": {"implemented": True, "production_wired": True, "configured": False},
    }
    assert service.close()


def test_budget_evicts_lowest_utility_and_protects_seed_and_insight(
    monkeypatch, no_op_consolidation
):
    monkeypatch.setenv("AUTOPOIESIS_MEMORY_BUDGET", "3")
    repository = _SnapshotRepository(
        [
            _record("low", strength=0.1, importance=0.0),
            _record("high", strength=1.0, importance=10.0, access_count=10),
            _record("seed-prior", tags=["seed"], strength=0.8),
            _record("derived-insight", tags=["insight"], strength=0.7),
            _checkpoint(NOW),
        ]
    )
    service = _service(repository)

    service.diagnose(CASE)

    assert {record.memory_id for record in service.memory.active()} == {
        "high",
        "seed-prior",
        "derived-insight",
    }
    low = service.memory.get("low")
    assert low is not None and low.quarantined
    assert "quarantine:evicted" in low.tags
    assert service.memory.get("seed-prior").strength == 0.8
    assert service.memory.get("derived-insight").strength == 0.7
    assert service.close()


def test_ten_diagnoses_in_one_interval_apply_one_decay_tick(
    monkeypatch, no_op_consolidation
):
    operations: list[dict] = []
    repository = _SnapshotRepository([_record("aging")])
    service = _service(repository, consolidation_options={"recorder": operations})
    instants = iter(NOW + timedelta(hours=offset) for offset in range(10))
    monkeypatch.setattr(service_module, "_utc_now", lambda: next(instants))

    for _ in range(10):
        service.diagnose(CASE)

    aging = service.memory.get("aging")
    assert aging is not None and aging.strength == pytest.approx(0.55)
    assert [item["op"] for item in operations] == ["DECAY"]
    checkpoint = service.memory.get(service_module._DECAY_CHECKPOINT_ID)
    assert checkpoint is not None and checkpoint.last_observed_at == NOW
    assert service.close()


def test_decay_checkpoint_survives_service_recreation(
    monkeypatch, no_op_consolidation
):
    monkeypatch.setenv("AUTOPOIESIS_MEMORY_DECAY_INTERVAL", "7200")
    repository = _SnapshotRepository([_record("aging")])
    first_operations: list[dict] = []
    first = _service(repository, consolidation_options={"recorder": first_operations})
    first.diagnose(CASE)
    assert first.close()

    monkeypatch.setattr(service_module, "_utc_now", lambda: NOW + timedelta(hours=1))
    second_operations: list[dict] = []
    second = _service(repository, consolidation_options={"recorder": second_operations})
    assert second.health()["memory_retention"]["decay_interval_seconds"] == 7200.0
    second.diagnose(CASE)

    aging = second.memory.get("aging")
    assert aging is not None and aging.strength == pytest.approx(0.55)
    assert [item["op"] for item in first_operations] == ["DECAY"]
    assert second_operations == []
    assert second.close()


def test_consolidation_decay_eviction_and_checkpoint_share_one_failed_write(
    monkeypatch,
):
    monkeypatch.setenv("AUTOPOIESIS_MEMORY_BUDGET", "1")
    initial = [_record("existing"), _record("seed-prior", tags=["seed"])]
    repository = _SnapshotRepository(initial)
    service = _service(repository)

    def consolidate(events, case, memory, skills, evidence, **_options):
        del events, skills, evidence
        memory.add(_record("learned"))
        memory.flush()
        return ConsolidationReport(run_id=case.id, passed=True, added=["learned"])

    monkeypatch.setattr(consolidate_module, "consolidate_run", consolidate)
    repository.fail_writes = True

    _diagnosis, verification = service.diagnose(CASE)

    assert verification.passed
    assert len(repository.attempted) == 1
    attempted = {record.memory_id: record for record in repository.attempted[0]}
    assert set(attempted) == {
        "existing",
        "learned",
        "seed-prior",
        service_module._DECAY_CHECKPOINT_ID,
    }
    assert attempted["existing"].strength == pytest.approx(0.55)
    assert attempted["learned"].strength == pytest.approx(0.55)
    assert attempted["existing"].quarantined
    assert attempted["learned"].quarantined
    assert attempted["seed-prior"].strength == 1.0
    assert attempted[service_module._DECAY_CHECKPOINT_ID].last_observed_at == NOW

    assert _payloads(service.memory.records()) == _payloads(initial)
    assert _payloads(repository.stored) == _payloads(initial)
    assert service.last_consolidation is None
    assert "retention transaction failed" in service.health()["last_error"]
    assert service.close()
