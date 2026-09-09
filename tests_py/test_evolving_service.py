from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.memory.store import TieredMemoryStore
from core.orchestrator.evolving_service import EvolvingRCAService
from domains.network_rca import build_network_rca_service
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases


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


def test_network_service_factory_exports_the_closed_loop_service(tmp_path):
    service = build_network_rca_service(
        tmp_path / "service-trace.jsonl",
        seed_memory=False,
        start_maintenance=False,
    )
    assert isinstance(service, EvolvingRCAService)
    assert service.close()


def test_default_maintenance_only_creates_enabled_index_workers():
    class _Runtime:
        def __init__(self, memory):
            self.memory = memory
            self.skills = SimpleNamespace(all=lambda: [])

    disabled = EvolvingRCAService(
        _Runtime(TieredMemoryStore(enabled=False)),
        start_maintenance=False,
    )
    lexical_only = EvolvingRCAService(
        _Runtime(TieredMemoryStore()),
        start_maintenance=False,
    )

    assert disabled._maintenance_workers == []
    assert [worker.name for worker in lexical_only._maintenance_workers] == [
        "memory-bm25-maintenance"
    ]
    assert disabled.close()
    assert lexical_only.close()
