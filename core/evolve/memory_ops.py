"""Phase B — richer memory dynamics on top of the Phase A self-evolution loop.

The persistent core is *managed*, not merely appended to. Four rule-based
mechanisms (no LLM in the hot path), each drawn from the recent agent-memory
literature and each derived from real run signals — nothing here is synthesized:

  * router     — Mem0-style ADD / UPDATE / NOOP write decision, so re-observing a
                 pattern reinforces the existing memory instead of duplicating it.
                 (Mem0: Chhikara et al., 2025)
  * links      — A-MEM associative links between same-family memories, so recall
                 can hop from a weak surface hit to a strong neighbour — the basis
                 for generalizing to a novel-but-similar incident.
                 (A-MEM: Xu et al., 2025)
  * reflection — importance-gated synthesis: once an incident *family* accrues
                 enough salience, abstract it into a higher-level semantic insight
                 that links its members.
                 (Generative Agents: Park et al., 2023)
  * decay      — Ebbinghaus retrievability decay + forgetting: a memory not reused
                 loses strength each tick and is pruned below a floor; reuse resets
                 it. Keeps the store bounded and drops stale one-offs.
                 (Ebbinghaus, 1885)
"""
from __future__ import annotations

from dataclasses import dataclass

from core.memory.store import MemoryRecord, TieredMemoryStore

_SKIP_PREFIX = ("skill:", "quarantine:", "root:")


def _tagset(tags) -> set[str]:
    """Content tags only — bookkeeping tags (skill:/root:/quarantine:/insight) don't
    describe the incident and would distort family similarity."""
    return {t.lower() for t in tags if not t.startswith(_SKIP_PREFIX) and t != "insight"}


def similarity(a_tags, a_assets, b_tags, b_assets) -> float:
    """Family similarity in [0,1] from shared content tags + shared assets."""
    at, bt = _tagset(a_tags), _tagset(b_tags)
    aa, ba = set(a_assets), set(b_assets)
    tag_j = len(at & bt) / len(at | bt) if (at | bt) else 0.0
    asset_j = len(aa & ba) / len(aa | ba) if (aa | ba) else 0.0
    return round(0.6 * tag_j + 0.4 * asset_j, 4)


@dataclass
class RouteDecision:
    op: str                       # "ADD" | "UPDATE" | "NOOP"
    target_id: str | None = None
    similarity: float = 0.0


def route(memory: TieredMemoryStore, candidate: MemoryRecord, *, update_thresh: float = 0.62, noop_thresh: float = 0.97) -> RouteDecision:
    """Mem0-style write router: is a candidate memory genuinely new (ADD), a variant
    of one we already hold (UPDATE — reinforce + merge), or already fully captured
    (NOOP)? Compared only within the same tier."""
    best: MemoryRecord | None = None
    best_sim = 0.0
    for rec in memory._records:
        if rec.tier != candidate.tier or rec.quarantined or rec.memory_id == candidate.memory_id:
            continue
        s = similarity(candidate.tags, candidate.asset_ids, rec.tags, rec.asset_ids)
        if s > best_sim:
            best, best_sim = rec, s
    if best is None or best_sim < update_thresh:
        return RouteDecision("ADD", None, best_sim)
    new_info = bool((_tagset(candidate.tags) - _tagset(best.tags)) or (set(candidate.asset_ids) - set(best.asset_ids)))
    if best_sim >= noop_thresh and not new_info:
        return RouteDecision("NOOP", best.memory_id, best_sim)
    return RouteDecision("UPDATE", best.memory_id, best_sim)


def apply_route(memory: TieredMemoryStore, candidate: MemoryRecord, decision: RouteDecision, *, conf_cap: float = 3.0) -> str:
    """Execute a RouteDecision; returns the memory_id that now holds the information."""
    if decision.op == "ADD":
        candidate.strength = 1.0
        memory.add(candidate)
        return candidate.memory_id
    target = memory.get(decision.target_id) if decision.target_id else None
    if target is None:                       # target vanished (forgotten) — treat as ADD
        candidate.strength = 1.0
        memory.add(candidate)
        return candidate.memory_id
    target.strength = 1.0                     # any hit refreshes retrievability
    if decision.op == "UPDATE":
        for t in candidate.tags:
            if t not in target.tags:
                target.tags.append(t)
        for a in candidate.asset_ids:
            if a not in target.asset_ids:
                target.asset_ids.append(a)
        target.confidence = min(conf_cap, target.confidence + 0.3)
        target.importance += 1.0
        target.source_trace_ids.extend(t for t in candidate.source_trace_ids if t not in target.source_trace_ids)
        if candidate.evidence_snapshot and not target.evidence_snapshot:
            target.evidence_snapshot = candidate.evidence_snapshot
    return target.memory_id


def link_related(memory: TieredMemoryStore, record: MemoryRecord, *, k: int = 3, thresh: float = 0.34) -> list[str]:
    """A-MEM: connect a memory bidirectionally to its k most similar neighbours, so
    recall can traverse family links from a weak hit to a strong one."""
    scored: list[tuple[float, MemoryRecord]] = []
    for rec in memory._records:
        if rec.memory_id == record.memory_id or rec.quarantined:
            continue
        s = similarity(record.tags, record.asset_ids, rec.tags, rec.asset_ids)
        if s >= thresh:
            scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    linked = []
    for _, rec in scored[:k]:
        if rec.memory_id not in record.links:
            record.links.append(rec.memory_id)
        if record.memory_id not in rec.links:
            rec.links.append(record.memory_id)
        linked.append(rec.memory_id)
    return linked


def neighbours(memory: TieredMemoryStore, record: MemoryRecord) -> list[MemoryRecord]:
    """Resolve a record's A-MEM links to live (non-quarantined) records."""
    return [n for mid in record.links if (n := memory.get(mid)) is not None and not n.quarantined]


def reflect(memory: TieredMemoryStore, *, importance_thresh: float = 3.0, min_members: int = 2) -> list[str]:
    """Generative-Agents reflection: when an incident family (grouped by primary
    asset) has accrued enough salience across >= min_members episodic incidents,
    synthesize a higher-level semantic *insight* that names the family, records its
    distinct root causes, and links its members. Idempotent per family."""
    families: dict[str, list[MemoryRecord]] = {}
    for rec in memory._records:
        if rec.tier == "episodic" and not rec.quarantined and rec.asset_ids:
            families.setdefault(rec.asset_ids[0], []).append(rec)

    created: list[str] = []
    for fam, members in families.items():
        if len(members) < min_members:
            continue
        salience = sum(m.importance + m.confidence for m in members)
        insight_id = f"insight-{fam}"
        existing = memory.get(insight_id)
        if salience < importance_thresh:
            continue
        roots = sorted({t[len("root:"):] for m in members for t in m.tags if t.startswith("root:")})
        member_ids = [m.memory_id for m in members]
        if existing is not None:                       # keep an existing insight current
            existing.strength = 1.0
            existing.importance = salience
            for mid in member_ids:
                if mid not in existing.links:
                    existing.links.append(mid)
            for r in roots:
                if r not in existing.tags:
                    existing.tags.append(r)
            continue
        insight = MemoryRecord(
            memory_id=insight_id, tier="semantic",
            text=f"family {fam}: {len(members)} incident patterns — {', '.join(roots) or 'mixed'}",
            tags=[fam, "insight", *roots], asset_ids=[fam],
            confidence=1.0 + 0.2 * len(members), importance=salience,
            links=list(member_ids),
        )
        memory.add(insight)
        for m in members:
            if insight_id not in m.links:
                m.links.append(insight_id)
        created.append(insight_id)
    return created


def decay_and_forget(memory: TieredMemoryStore, *, retention: float = 0.55, floor: float = 0.4, protect: tuple[str, ...] = ("seed", "asset", "insight")) -> list[str]:
    """Ebbinghaus tick: every non-protected active memory loses retrievability;
    those below the floor are forgotten (quarantined). Memories reused this tick were
    reset to strength 1.0 and survive; a memory unused for ~2 ticks fades out.
    Protected priors (seeded / asset-profile / reflected insights) never decay."""
    forgotten: list[str] = []
    for rec in memory._records:
        if rec.quarantined or rec.memory_id.startswith(protect):
            continue
        rec.strength *= retention
        if rec.strength < floor:
            memory.quarantine(rec.memory_id, "forgotten")
            forgotten.append(rec.memory_id)
    return forgotten


def memory_health(memory: TieredMemoryStore) -> dict:
    """A compact, honest snapshot of store health for measurement / the UI."""
    active = memory.active()
    by_tier: dict[str, int] = {}
    for r in active:
        by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
    return {
        "active": len(active),
        "forgotten": sum(1 for r in memory._records if r.quarantined),
        "insights": sum(1 for r in active if "insight" in r.tags),
        "links": sum(len(r.links) for r in active),
        "by_tier": by_tier,
    }
