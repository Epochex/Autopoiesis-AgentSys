"""Phase B — the memory-management mechanisms are real, safe, and do what the
papers they cite claim. Each test guards one mechanism's defining property.

  router  — Mem0 (Chhikara+ 2025)          reinforce, don't duplicate
  links   — A-MEM (Xu+ 2025)               same-family memories connect
  reflect — Generative Agents (Park+ 2023) salient families abstract upward
  decay   — Ebbinghaus (1885)              unused memories fade, reused persist
"""
from __future__ import annotations

from core.evolve import (
    apply_route,
    decay_and_forget,
    link_related,
    memory_health,
    neighbours,
    reflect,
    route,
    similarity,
)
from core.memory.store import MemoryRecord, TieredMemoryStore


def _epi(mid, terms, assets, root, conf=0.9):
    return MemoryRecord(
        memory_id=mid, tier="episodic", text=f"{mid} -> {root}",
        tags=[*terms, root, f"root:{root}"], asset_ids=assets, confidence=conf,
    )


# ── router: Mem0 ADD / UPDATE / NOOP ─────────────────────────────────────────
def test_router_adds_a_genuinely_new_incident():
    mem = TieredMemoryStore()
    d = route(mem, _epi("a", ["carrier", "eno1"], ["r230", "eno1"], "carrier_down"))
    assert d.op == "ADD"


def test_router_updates_a_variant_instead_of_duplicating():
    mem = TieredMemoryStore()
    apply_route(mem, _epi("a", ["carrier", "interface", "eno1"], ["r230", "eno1"], "carrier_down"), route(mem, _epi("a", ["carrier", "interface", "eno1"], ["r230", "eno1"], "carrier_down")))
    # a near-identical re-observation with one new tag → UPDATE the prior, no dup
    variant = _epi("b", ["carrier", "interface", "eno1", "sfp"], ["r230", "eno1"], "carrier_down")
    d = route(mem, variant)
    assert d.op == "UPDATE" and d.target_id == "a"
    held = apply_route(mem, variant, d)
    assert held == "a"
    assert len(mem.active()) == 1                       # not duplicated
    assert "sfp" in mem.get("a").tags                   # merged the new info
    assert mem.get("a").importance > 1.0                # reinforced


def test_router_noops_on_an_identical_re_observation():
    mem = TieredMemoryStore()
    rec = _epi("a", ["carrier", "eno1"], ["r230", "eno1"], "carrier_down")
    apply_route(mem, rec, route(mem, rec))
    dup = _epi("b", ["carrier", "eno1"], ["r230", "eno1"], "carrier_down")
    d = route(mem, dup)
    assert d.op == "NOOP"
    assert len(mem.active()) == 1


# ── A-MEM associative links ──────────────────────────────────────────────────
def test_amem_links_same_family_bidirectionally():
    mem = TieredMemoryStore()
    a = _epi("a", ["carrier", "interface"], ["r230", "eno1"], "carrier_down")
    b = _epi("b", ["carrier", "interface"], ["r230", "eno3"], "carrier_down")  # same family
    far = _epi("c", ["fortiguard", "av"], ["fortigate", "wan1"], "sub_expired")  # unrelated
    for r in (a, b, far):
        mem.add(r)
    linked = link_related(mem, b)
    assert "a" in linked and "c" not in linked
    assert "b" in mem.get("a").links and "a" in mem.get("b").links   # bidirectional


# ── Generative-Agents reflection ─────────────────────────────────────────────
def test_reflection_abstracts_a_salient_family_and_is_idempotent():
    mem = TieredMemoryStore()
    mem.add(_epi("a", ["carrier", "interface"], ["r230", "eno1"], "carrier_down", conf=1.2))
    mem.add(_epi("b", ["rx", "dropped"], ["r230", "eno2"], "benign_rx_dropped", conf=1.2))
    created = reflect(mem)
    assert created == ["insight-r230"]
    ins = mem.get("insight-r230")
    assert ins.tier == "semantic" and "insight" in ins.tags
    assert set(ins.links) == {"a", "b"}                              # links its members
    assert "carrier_down" in ins.tags and "benign_rx_dropped" in ins.tags  # names roots
    assert reflect(mem) == []                                        # idempotent — no dup


def test_reflection_stays_silent_below_the_member_threshold():
    mem = TieredMemoryStore()
    mem.add(_epi("a", ["carrier"], ["r230", "eno1"], "carrier_down"))
    assert reflect(mem) == []
    assert mem.get("insight-r230") is None


# ── Ebbinghaus decay + forgetting ────────────────────────────────────────────
def test_decay_forgets_stale_one_off_keeps_reused_and_protects_priors():
    mem = TieredMemoryStore()
    reused = _epi("epi-reused", ["carrier"], ["r230", "eno1"], "carrier_down")
    stale = _epi("epi-stale", ["vip"], ["fortigate", "wan1"], "vip_mismatch")
    seed = MemoryRecord(memory_id="seed-x", tier="semantic", text="prior", confidence=2.0)
    for r in (reused, stale, seed):
        mem.add(r)
    # tick 1: 'reused' is refreshed each tick (as consolidation would), 'stale' is not
    for _ in range(2):
        mem.get("epi-reused").strength = 1.0
        decay_and_forget(mem)
    assert mem.get("epi-reused") in mem.active()        # survives — kept warm
    assert mem.get("epi-stale").quarantined             # faded out after ~2 idle ticks
    assert mem.get("seed-x") in mem.active()            # protected prior never decays


# ── generalization mechanism (SYNTHETIC, clearly labelled) ───────────────────
def test_family_link_enables_transfer_to_a_novel_but_similar_incident():
    """SYNTHETIC mechanism demo (not a real-log number): the real R230 held-out set
    has no two incidents sharing a root cause, so exact episodic recall can't transfer.
    This constructs a matured `carrier_down` family (episodic + procedural + insight)
    and shows a *novel* incident of the same root (different interface eno9) links into
    the family and can reach its procedural shortcut — the path Phase A's exact-match
    recall would miss. It demonstrates the wiring, and is honest about being synthetic."""
    mem = TieredMemoryStore()
    known = _epi("epi-eno1", ["carrier", "interface", "eno1"], ["r230", "eno1"], "carrier_down", conf=1.2)
    mem.add(known)
    proc = MemoryRecord(
        memory_id="proc-carrier_down", tier="procedural", text="for carrier_down, probe link_state",
        tags=["carrier", "interface", "carrier_down", "root:carrier_down", "skill:link_state"],
        asset_ids=["r230", "eno1"], confidence=1.6,
    )
    mem.add(proc)
    link_related(mem, proc)                              # family cohesion: proc ↔ episodic

    # a NOVEL incident: same root, unseen interface → weak exact overlap, strong family sim
    novel = _epi("epi-eno9", ["carrier", "interface", "eno9"], ["r230", "eno9"], "carrier_down", conf=0.9)
    assert similarity(novel.tags, novel.asset_ids, known.tags, known.asset_ids) >= 0.34
    mem.add(novel)
    fam = link_related(mem, novel)
    assert "epi-eno1" in fam                             # links into the family...
    reachable = {n.memory_id for n in neighbours(mem, novel)}
    reachable |= {n.memory_id for m in reachable for n in neighbours(mem, mem.get(m))}
    assert "proc-carrier_down" in reachable             # ...and can reach the family's shortcut


def test_memory_health_snapshot_is_consistent():
    mem = TieredMemoryStore()
    mem.add(_epi("a", ["carrier"], ["r230", "eno1"], "carrier_down"))
    mem.add(_epi("b", ["rx"], ["r230", "eno2"], "benign_rx_dropped"))
    reflect(mem)
    h = memory_health(mem)
    assert h["active"] == 3 and h["insights"] == 1 and h["by_tier"]["episodic"] == 2
