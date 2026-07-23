"""End-to-end coverage for the deterministic memory lifecycle.

This test deliberately uses the consolidation boundary rather than calling each
mechanism in isolation.  Synthetic trace events drive the same write router,
association, reflection, conflict-resolution, eviction, observatory, and BM25
store paths used by an evolving run.  No orchestrator, model, fixture, or network
access is involved.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import core.evolve.consolidate as consolidate_module
from core.evolve.consolidate import consolidate_run
from core.evolve.memory_ops import memory_health, utility_evict, utility_scores
from core.evolve.observatory import CAPABILITIES, quarantine_reason
from core.memory.store import TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent


@dataclass
class _Case:
    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str] = field(default_factory=list)


class _FixedUUID:
    def __init__(self, suffix: str):
        self.hex = suffix


def _events(case: _Case, root: str, sequence: int) -> list[TraceEvent]:
    """The minimal verified-run trace consumed by ``consolidate_run``."""
    run_id = f"run-{sequence}"
    return [
        TraceEvent(
            run_id=run_id,
            case_id=case.id,
            kind="memory_read",
            payload={"episodic": []},
        ),
        TraceEvent(
            run_id=run_id,
            case_id=case.id,
            kind="skills_exposed",
            payload={"skills": []},
        ),
        TraceEvent(
            run_id=run_id,
            case_id=case.id,
            kind="verifier_result",
            payload={"passed": True},
        ),
        TraceEvent(
            run_id=run_id,
            case_id=case.id,
            kind="diagnosis_completed",
            payload={
                "root_cause_key": root,
                "confidence": 0.95,
                "evidence": [{"evidence_id": f"ev-{sequence}"}],
            },
        ),
    ]


def _consolidate(
    case: _Case,
    root: str,
    sequence: int,
    memory: TieredMemoryStore,
    skills: SkillRegistry,
    events: list[dict],
    *,
    resolve_conflicts: bool = False,
) -> None:
    consolidate_run(
        _events(case, root, sequence),
        case,
        memory,
        skills,
        [{"evidence_id": f"ev-{sequence}", "source": "synthetic", "summary": "observed"}],
        resolve_conflicts=resolve_conflicts,
        recorder=events,
    )


def test_full_memory_lifecycle_is_observable_and_retrievable(monkeypatch):
    # consolidate_run normally gives episodic records random suffixes.  Fixing them
    # makes both the supersession provenance and operation stream reproducible.
    fixed_uuids = iter(_FixedUUID(f"{i:06d}") for i in range(1, 20))
    monkeypatch.setattr(consolidate_module, "uuid4", lambda: next(fixed_uuids))

    memory = TieredMemoryStore()
    skills = SkillRegistry()
    operations: list[dict] = []

    carrier = _Case(
        "carrier-1",
        "carrier interface fault on router",
        ["carrier", "interface", "fault"],
        ["r230"],
    )

    # New knowledge takes the ADD route.  Re-observing it is deduplicated at the
    # episodic layer and emits the semantic layer's real REINFORCE mutation.
    _consolidate(carrier, "carrier_down", 1, memory, skills, operations)
    _consolidate(
        _Case("carrier-repeat", carrier.query, carrier.query_terms, carrier.assets),
        "carrier_down",
        2,
        memory,
        skills,
        operations,
    )

    # A distinct incident on the same asset is ADDed, A-MEM links the family,
    # and the importance/member gate creates the family's first insight.
    _consolidate(
        _Case("link-1", "link oscillation on router", ["link", "oscillation", "flap"], ["r230"]),
        "link_flap",
        3,
        memory,
        skills,
        operations,
    )

    # A third member changes the mature family and exercises the idempotent
    # reflection-refresh path rather than creating a duplicate insight.
    _consolidate(
        _Case("power-1", "power loss on router", ["power", "loss", "supply"], ["r230"]),
        "power_loss",
        4,
        memory,
        skills,
        operations,
    )

    # The same carrier observation now carries a different diagnosed root.  With
    # conflict resolution wired, this retires the stale episode and preserves the
    # replacement provenance instead of merging contradictory roots.
    _consolidate(
        _Case("carrier-renamed", carrier.query, carrier.query_terms, carrier.assets),
        "sfp_fault",
        5,
        memory,
        skills,
        operations,
        resolve_conflicts=True,
    )

    old_id = "epi-carrier-1-000001"
    replacement_id = "epi-carrier-renamed-000005"
    old = memory.get(old_id)
    replacement = memory.get(replacement_id)
    assert old is not None and old.quarantined
    assert quarantine_reason(old) == "superseded"
    assert old.superseded_by == replacement_id
    assert replacement is not None and old_id in replacement.links

    # The budget is below the active population, so the two lowest-utility,
    # unprotected records are genuinely evicted.  The reflected insight is protected.
    scores = utility_scores(memory)
    assert scores["sem-carrier_down"] == max(scores.values())
    evicted = utility_evict(memory, budget=6, recorder=operations)
    assert set(evicted) == {"sem-power_loss", "sem-sfp_fault"}
    assert memory.get("insight-r230") in memory.active()

    # The events expose each lifecycle stage in causal order.  LINK is emitted for
    # both sides of the association; checking the first occurrence is sufficient.
    op_names = [event["op"] for event in operations]
    expected_stages = [
        "ADD",
        "REINFORCE",
        "LINK",
        "INSIGHT",
        "INSIGHT_REFRESH",
        "SUPERSEDE",
        "EVICT",
    ]
    positions = [op_names.index(stage) for stage in expected_stages]
    assert positions == sorted(positions)

    supersede_op = next(event for event in operations if event["op"] == "SUPERSEDE")
    assert supersede_op["memory_id"] == old_id
    assert supersede_op["target_id"] == replacement_id
    assert supersede_op["before"] != supersede_op["after"]
    assert {event["memory_id"] for event in operations if event["op"] == "EVICT"} == set(evicted)

    assert CAPABILITIES["decay_wired"] is True
    assert CAPABILITIES["eviction_wired"] is True
    assert CAPABILITIES["conflict_update_wired"] is True
    assert memory_health(memory) == {
        "active": 6,
        "forgotten": 3,  # one superseded record plus two utility evictions
        "insights": 1,
        "links": 22,  # associative edges plus adjacent same-asset chronology
        "by_tier": {"semantic": 3, "episodic": 3},
        "index": memory.index_health(),
    }

    # This is the store's zero-dependency BM25 path, with structural reranking left
    # enabled.  Its highest-utility evictable record survives and is the top hit;
    # quarantined and evicted records cannot leak back into any retrieval tier.
    recalled = memory.retrieve(["carrier_down"], [], limit_per_tier=10)
    recalled_ids = [record.memory_id for tier in recalled.values() for record in tier]
    assert recalled["semantic"][0].memory_id == "sem-carrier_down"
    assert old_id not in recalled_ids
    assert not set(evicted).intersection(recalled_ids)
