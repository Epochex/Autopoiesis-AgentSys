from __future__ import annotations

from dataclasses import dataclass

from core.evolve.consolidate import consolidate_run
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent


@dataclass
class _Case:
    id: str = "case-1"
    query: str = "diagnose uplink"
    query_terms: tuple[str, ...] = ("uplink",)
    assets: tuple[str, ...] = ("router-1",)


def _events(
    run_id: str,
    *,
    passed: bool,
    cited: bool = True,
    blocked: bool = False,
) -> list[TraceEvent]:
    evidence_ids = [f"ev-{run_id}"]
    return [
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="memory_read",
            payload={"episodic": ["memory-a", "memory-b"]},
        ),
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="context_compiled",
            payload={"included_memory_ids": ["memory-a", "memory-b", "memory-a"]},
        ),
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="memory_attributed",
            payload={
                "memory_ids": ["memory-a"],
                "items": [{"memory_id": "memory-a", "role": "episodic_hypothesis"}],
            },
        ),
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="tool_called",
            payload={"skill": "probe", "evidence_ids": evidence_ids, "blocked": blocked},
        ),
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="verifier_result",
            payload={"passed": passed},
        ),
        TraceEvent(
            run_id=run_id,
            case_id="case-1",
            kind="diagnosis_completed",
            payload={
                "root_cause_key": "new_root" if passed else "unknown",
                "confidence": 0.9 if passed else 0.0,
                "evidence": [{"evidence_id": evidence_ids[0]}] if cited else [],
            },
        ),
    ]


def _memory() -> TieredMemoryStore:
    memory = TieredMemoryStore()
    memory.seed(
        [
            MemoryRecord(
                memory_id="memory-a",
                tier="episodic",
                text="old root A",
                tags=["root:old_root"],
            ),
            MemoryRecord(
                memory_id="memory-b",
                tier="episodic",
                text="other retrieved candidate",
                tags=["root:other_root"],
            ),
        ]
    )
    return memory


def _evidence(run_id: str) -> list[dict]:
    return [
        {
            "evidence_id": f"ev-{run_id}",
            "source": "fresh_probe",
            "summary": "the old root is disproved",
            "contradicts": "old_root",
        }
    ]


def test_only_included_and_explicitly_attributed_memory_receives_positive_credit():
    memory = _memory()
    events = _events("run-positive", passed=True)
    events.insert(
        -1,
        TraceEvent(
            run_id="run-positive",
            case_id="case-1",
            kind="memory_resolved",
            payload={"memory_id": "memory-a", "freshness_verified": True},
        ),
    )

    report = consolidate_run(
        events,
        _Case(),
        memory,
        SkillRegistry(),
        [{"evidence_id": "ev-run-positive", "source": "fresh_probe", "summary": "new root"}],
    )

    memory_a = memory.get("memory-a")
    memory_b = memory.get("memory-b")
    assert report.accessed == ["memory-a", "memory-b"]
    assert report.reinforced == ["memory-a"]
    assert memory_a is not None and memory_a.access_count == 1 and memory_a.confidence == 1.1
    assert memory_b is not None and memory_b.access_count == 1 and memory_b.confidence == 1.0


def test_uncited_or_failed_tool_evidence_never_punishes_retrieved_memories():
    for cited, blocked in ((False, False), (True, True)):
        memory = _memory()
        report = consolidate_run(
            _events("run-failed", passed=False, cited=cited, blocked=blocked),
            _Case(),
            memory,
            SkillRegistry(),
            _evidence("run-failed"),
        )

        assert report.contradiction_strikes == []
        assert report.quarantined == []
        assert all(not record.quarantined for record in memory.records())


def test_two_independent_explicit_contradictions_quarantine_only_the_attributed_episode():
    memory = _memory()

    first = consolidate_run(
        _events("run-one", passed=False),
        _Case(),
        memory,
        SkillRegistry(),
        _evidence("run-one"),
    )
    replay = consolidate_run(
        _events("run-one", passed=False),
        _Case(),
        memory,
        SkillRegistry(),
        _evidence("run-one"),
    )
    second = consolidate_run(
        _events("run-two", passed=False),
        _Case(),
        memory,
        SkillRegistry(),
        _evidence("run-two"),
    )

    memory_a = memory.get("memory-a")
    memory_b = memory.get("memory-b")
    assert first.contradiction_strikes == ["memory-a"] and first.quarantined == []
    assert replay.contradiction_strikes == [] and replay.quarantined == []
    assert second.contradiction_strikes == ["memory-a"]
    assert second.quarantined == ["memory-a"]
    assert memory_a is not None and memory_a.quarantined
    assert "quarantine:repeated_explicit_contradiction" in memory_a.tags
    assert memory_b is not None and not memory_b.quarantined
    assert memory_a.access_count == 2 and memory_b.access_count == 2
