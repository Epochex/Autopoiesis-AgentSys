from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from core.memory.store import MemoryRecord, TieredMemoryStore
from core.orchestrator.evolving_service import EvolvingRCAService
from core.skills.registry import SkillRegistry
from domains.network_rca.incident_memory import consolidate_incident_timeline


def _passed_chain(detector: str, *, minute: int) -> list[dict]:
    subject = "edge-api.service"
    action = "restart_unit"
    prefix = f"2026-08-22T10:{minute:02d}"
    return [
        {
            "at": f"{prefix}:00+00:00",
            "kind": "detected",
            "detector": detector,
            "family": "service_health",
            "subject": subject,
            "target": subject,
            "summary": f"service health changed on {subject}",
            "action": action,
        },
        {
            "at": f"{prefix}:01+00:00",
            "kind": "remediated",
            "detector": detector,
            "subject": subject,
            "action": action,
            "outcome": "passed",
        },
        {
            "at": f"{prefix}:02+00:00",
            "kind": "resolved",
            "detector": detector,
            "subject": subject,
            "action": action,
            "outcome": "passed",
        },
    ]


def test_sentinel_consolidation_calls_retention_inside_existing_lock(monkeypatch):
    from frontend.gateway.app import sentinel_wiring

    calls: list[str] = []
    request_lock = threading.Lock()

    class Memory:
        repository = object()

        def flush(self) -> None:
            assert request_lock.locked()
            calls.append("flush")

    class Service:
        memory = Memory()
        skills = SkillRegistry()
        _request_lock = request_lock

        def _apply_memory_retention(self, *, now: datetime) -> dict:
            assert request_lock.locked()
            assert now.tzinfo is timezone.utc
            calls.append("retention")
            return {}

    def consolidate(_timeline, memory, skills):
        assert request_lock.locked()
        assert memory is Service.memory
        assert skills is Service.skills
        calls.append("consolidate")
        return []

    monkeypatch.setattr(sentinel_wiring, "_resolve_learning_service", Service)
    monkeypatch.setattr(sentinel_wiring, "timeline", lambda _limit: [])
    monkeypatch.setattr(sentinel_wiring, "consolidate_incident_timeline", consolidate)

    sentinel_wiring._remember_completed_incidents()

    assert calls == ["consolidate", "retention", "flush"]
    assert not request_lock.locked()


def test_sentinel_memory_sync_never_blocks_the_next_detection_cycle(monkeypatch):
    from frontend.gateway.app import sentinel_wiring

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_memory_sync() -> None:
        calls.append("memory")
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(
        sentinel_wiring,
        "_remember_completed_incidents",
        slow_memory_sync,
    )
    sentinel = sentinel_wiring._LearningSentinel(
        detectors=[lambda: []],
        execute=lambda *_args, **_kwargs: {},
        preflight=lambda *_args, **_kwargs: {},
    )

    started = time.monotonic()
    first = sentinel.poll_once()
    elapsed = time.monotonic() - started
    assert first["busy"] is False
    assert elapsed < 0.2
    assert entered.wait(timeout=1)

    second = sentinel.poll_once()
    assert second["busy"] is False
    assert calls == ["memory"]

    release.set()
    deadline = time.monotonic() + 1
    while sentinel_wiring._memory_sync_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not sentinel_wiring._memory_sync_lock.locked()


def test_sentinel_root_change_supersedes_old_incident_without_merged_roots():
    memory = TieredMemoryStore()
    registry = SkillRegistry()
    first_chain = _passed_chain("link_flap_old", minute=0)
    second_chain = _passed_chain("link_flap_new", minute=5)

    first = consolidate_incident_timeline(first_chain, memory, registry)
    old = next(record for record in memory.active() if record.tier == "episodic")
    second = consolidate_incident_timeline(second_chain, memory, registry)

    assert first and second
    assert second[0].superseded == [old.memory_id]
    retired = memory.get(old.memory_id)
    assert retired is not None
    assert retired.quarantined
    assert retired.superseded_by is not None
    live = memory.get(retired.superseded_by)
    assert live is not None and not live.quarantined
    assert "root:sentinel.link_flap_old" in retired.tags
    assert "root:sentinel.link_flap_new" not in retired.tags
    assert "root:sentinel.link_flap_new" in live.tags
    assert "root:sentinel.link_flap_old" not in live.tags
    assert all(
        len([tag for tag in record.tags if tag.startswith("root:")]) == 1
        for record in memory.records()
    )


def test_gateway_startup_wires_conflict_resolution_and_budget(monkeypatch):
    from domains.network_rca import factory, real_dataset
    from frontend.gateway.app import main

    captured: dict = {}

    class Service:
        def close(self) -> bool:
            return True

    def build_service(_ledger_path, **options):
        captured["budget"] = os.environ.get("AUTOPOIESIS_MEMORY_BUDGET")
        captured["options"] = options
        return Service()

    async def inline(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.delenv("AUTOPOIESIS_MEMORY_BUDGET", raising=False)
    monkeypatch.setattr(main, "_evolving_service", None)
    monkeypatch.setattr(main, "_start_prewarm", lambda: None)
    monkeypatch.setattr(main.asyncio, "to_thread", inline)
    monkeypatch.setattr(
        real_dataset,
        "validate_real_dataset_manifest",
        lambda _manifest: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(
        real_dataset,
        "load_real_case_bundle",
        lambda _manifest, split: ([SimpleNamespace(id="case-1")], {}),
    )
    monkeypatch.setattr(real_dataset, "resolve_stats_path", lambda _manifest: "stats")
    monkeypatch.setattr(factory, "build_network_rca_service", build_service)
    from frontend.gateway.app import sentinel_wiring

    monkeypatch.setattr(sentinel_wiring, "start_background", lambda: None)

    async def start_once() -> None:
        async with main._lifespan(main.app):
            assert main._runtime_error is None

    asyncio.run(start_once())

    assert captured["budget"] == "64"
    assert captured["options"]["consolidation_options"] == {
        "resolve_conflicts": True,
    }


def test_gateway_default_budget_triggers_real_eviction(monkeypatch):
    from frontend.gateway.app import main

    monkeypatch.delenv("AUTOPOIESIS_MEMORY_BUDGET", raising=False)
    main._configure_production_memory_budget()
    memory = TieredMemoryStore()
    for index in range(5):
        memory.add(MemoryRecord(
            memory_id=f"seed-{index}",
            tier="semantic",
            text=f"seed {index}",
            tags=["seed"],
        ))
    for index in range(61):
        memory.add(MemoryRecord(
            memory_id=f"learned-{index}",
            tier="episodic",
            text=f"learned {index}",
            strength=1.0,
            importance=float(index),
        ))
    orchestrator = SimpleNamespace(
        memory=memory,
        skills=SkillRegistry(),
        last_run_id="",
        _run_events=[],
        _last_evidence=[],
    )
    service = EvolvingRCAService(
        orchestrator,
        maintenance_workers=[],
        start_maintenance=False,
    )

    retention = service._apply_memory_retention(now=datetime.now(timezone.utc))

    assert service.health()["memory_retention"]["budget"] == 64
    assert len(retention["evicted"]) == 2
    assert len(memory.active()) == 64
    assert all(not memory.get(f"seed-{index}").quarantined for index in range(5))
    assert service.close()
