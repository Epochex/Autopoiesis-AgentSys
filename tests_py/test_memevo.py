"""The two wired memory-management mechanisms are real, fire in the loop, and behave.

  eviction — utility_evict(): capacity-budgeted, worth-ranked (not age alone), wired into
             run_evolving_stream; forgets the lowest-utility memories, protects priors.
  update   — route(resolve_conflicts=True) + supersede(): a memory that renames the root
             cause on the same entity retires the stale prior instead of merging it.

These guard the mechanism properties AND that each path actually fires in the kernel loop
(the point of the task: no dormant code, verified via the observatory op stream).
"""
from __future__ import annotations

from core.evolve import (
    UtilityWeights,
    apply_route,
    consolidate_run,
    route,
    run_evolving_stream,
    supersede,
    utility_evict,
    utility_scores,
)
from core.evolve.observatory import CAPABILITIES
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from domains.network_rca.factory import load_ground_truth, load_seed_cases


def _epi(mid, terms, assets, root, conf=0.9):
    return MemoryRecord(
        memory_id=mid, tier="episodic", text=f"{mid} -> {root}",
        tags=[*terms, root, f"root:{root}"], asset_ids=assets, confidence=conf,
    )


# ── BUILD 1: utility-driven eviction ─────────────────────────────────────────
def test_utility_ranks_by_worth_not_age_alone():
    """A memory that is old but reused + central beats a newer but idle, isolated one —
    proving utility is not a relabelled recency/decay."""
    mem = TieredMemoryStore()
    old_but_valuable = _epi("a", ["x"], ["dev"], "r1")
    old_but_valuable.strength = 0.1          # oldest
    old_but_valuable.access_count = 9        # heavily reused
    old_but_valuable.links = ["b", "c"]      # central
    old_but_valuable.importance = 5.0
    newer_but_idle = _epi("b", ["y"], ["dev2"], "r2")
    newer_but_idle.strength = 1.0            # newest
    for r in (old_but_valuable, newer_but_idle, _epi("c", ["z"], ["dev3"], "r3")):
        mem.add(r)
    scores = utility_scores(mem)
    assert scores["a"] > scores["b"]         # worth beats age


def test_utility_evict_binds_to_budget_and_protects_priors():
    mem = TieredMemoryStore()
    seed = MemoryRecord(memory_id="seed-x", tier="semantic", text="prior", confidence=2.0)
    mem.add(seed)
    for i in range(6):
        r = _epi(f"e{i}", [f"t{i}"], [f"d{i}"], f"root{i}")
        r.strength = i / 6.0
        r.access_count = i          # e0 is the weakest, e5 strongest
        mem.add(r)
    forgotten = utility_evict(mem, budget=4)
    active_ids = {r.memory_id for r in mem.active()}
    assert len(mem.active()) == 4
    assert "seed-x" in active_ids            # protected prior never evicted
    assert "e0" in forgotten and "e5" in active_ids   # lowest utility goes, highest stays
    # a store that already fits is a no-op
    assert utility_evict(mem, budget=10) == []


# ── BUILD 2: conflict-resolving update ───────────────────────────────────────
def test_route_supersedes_a_contradiction_but_only_when_asked():
    mem = TieredMemoryStore()
    apply_route(mem, _epi("a", ["carrier", "interface", "eno1"], ["r230", "eno1"], "carrier_down"),
                route(mem, _epi("a", ["carrier", "interface", "eno1"], ["r230", "eno1"], "carrier_down")))
    # same entity (r230/eno1, same terms) but a DIFFERENT diagnosed root cause
    contra = _epi("b", ["carrier", "interface", "eno1"], ["r230", "eno1"], "sfp_fault")
    assert route(mem, contra).op == "UPDATE"                        # default: merges (stale-prone)
    d = route(mem, contra, resolve_conflicts=True)
    assert d.op == "SUPERSEDE" and d.target_id == "a"              # opt-in: retires the prior


def test_supersede_retires_old_and_promotes_new_with_provenance():
    mem = TieredMemoryStore()
    mem.add(_epi("old", ["v"], ["dev"], "was"))
    new = _epi("new", ["v"], ["dev"], "now")
    ops: list[dict] = []
    held = supersede(mem, "old", new, recorder=ops)
    assert held == "new"
    assert mem.get("old").quarantined and mem.get("old").superseded_by == "new"
    assert "old" in mem.get("new").links                          # provenance link
    assert {r.memory_id for r in mem.active()} == {"new"}
    assert ops and ops[0]["op"] == "SUPERSEDE"


# ── both mechanisms actually FIRE in the consolidation / stream loop ──────────
def _contradiction_events(case_id, root):
    return [
        TraceEvent(run_id="r", case_id=case_id, kind="memory_read", payload={"episodic": []}),
        TraceEvent(run_id="r", case_id=case_id, kind="skills_exposed", payload={"skills": []}),
        TraceEvent(run_id="r", case_id=case_id, kind="verifier_result", payload={"passed": True}),
        TraceEvent(run_id="r", case_id=case_id, kind="diagnosis_completed",
                   payload={"root_cause_key": root, "confidence": 0.95,
                            "evidence": [{"evidence_id": "ev-1"}]}),
    ]


class _Case:
    def __init__(self, cid, root):
        self.id = cid
        self.query = f"why {root}"
        self.query_terms = ["carrier", "interface", "eno1"]
        self.assets = ["r230", "eno1"]


def test_conflict_resolving_update_fires_in_consolidation():
    mem = TieredMemoryStore()
    ev = [{"evidence_id": "ev-1", "source": "s", "summary": "obs"}]
    consolidate_run(_contradiction_events("c1", "carrier_down"), _Case("c1", "carrier_down"),
                    mem, SkillRegistry(), ev, resolve_conflicts=True)
    ops: list[dict] = []
    rep = consolidate_run(_contradiction_events("c2", "sfp_fault"), _Case("c2", "sfp_fault"),
                          mem, SkillRegistry(), ev, resolve_conflicts=True, recorder=ops)
    assert rep.superseded, "a re-diagnosis with a new root cause must SUPERSEDE the prior"
    assert any(o["op"] == "SUPERSEDE" for o in ops)


def test_utility_eviction_fires_in_the_stream_loop():
    cases, gt = load_seed_cases(), load_ground_truth()
    out = run_evolving_stream(cases, gt, passes=3, evolve=True, capacity_budget=1)
    assert CAPABILITIES["eviction_wired"] is True
    assert out["memory_health"]["forgotten"] > 0                  # eviction actually removed memories
    assert any(o["op"] == "EVICT" for o in out["observatory"]["events"])
    assert out["memory_health"]["active"] <= 1 + 3                # budget bound (allow protected priors)
