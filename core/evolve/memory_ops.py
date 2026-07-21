"""Phase B — richer memory dynamics on top of the Phase A self-evolution loop.

The learned memory state is *managed*, not merely appended to. Four rule-based
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
from typing import Iterable, Literal

from core.evolve.observatory import emit as _emit
from core.evolve.observatory import snapshot as _snap
from core.memory.store import MemoryRecord, TieredMemoryStore

_SKIP_PREFIX = ("skill:", "quarantine:", "root:")

# similarity() blend: content tags carry more family identity than shared assets.
_SIM_TAG_WEIGHT = 0.6
_SIM_ASSET_WEIGHT = 0.4

RouteOp = Literal["ADD", "UPDATE", "NOOP", "SUPERSEDE"]


def _emit_change(
    recorder: list[dict] | None,
    op: str,
    record: MemoryRecord,
    before: dict[str, object],
    **kw: object,
) -> None:
    """Record a mutation only if it actually mutated something.

    reflect() runs over every mature family on every pass and link_related re-walks
    neighbours, so both routinely re-apply a value that is already current. An op
    whose before == after changed nothing, so there is nothing to observe — emitting
    it anyway would pad the stream with events that explain no state change.
    """
    after = _snap(record)
    if after == before:
        return
    _emit(recorder, op, record.memory_id, record.tier, before=before, after=after, **kw)


def _tagset(tags: Iterable[str]) -> set[str]:
    """Content tags only — bookkeeping tags (skill:/root:/quarantine:/insight) don't
    describe the incident and would distort family similarity."""
    return {t.lower() for t in tags if not t.startswith(_SKIP_PREFIX) and t != "insight"}


def similarity(a_tags: Iterable[str], a_assets: Iterable[str], b_tags: Iterable[str], b_assets: Iterable[str]) -> float:
    """Family similarity in [0,1]: weighted Jaccard of content tags + shared assets."""
    at, bt = _tagset(a_tags), _tagset(b_tags)
    aa, ba = set(a_assets), set(b_assets)
    tag_j = len(at & bt) / len(at | bt) if (at | bt) else 0.0
    asset_j = len(aa & ba) / len(aa | ba) if (aa | ba) else 0.0
    return round(_SIM_TAG_WEIGHT * tag_j + _SIM_ASSET_WEIGHT * asset_j, 4)


@dataclass
class RouteDecision:
    op: RouteOp
    target_id: str | None = None
    similarity: float = 0.0


def _root_keys(tags: Iterable[str]) -> set[str]:
    """The diagnosed root-cause keys carried on a memory (``root:<key>`` tags). A memory
    can only *contradict* another about the same entity if it names a DIFFERENT root."""
    return {t[len("root:"):] for t in tags if t.startswith("root:")}


def route(
    memory: TieredMemoryStore,
    candidate: MemoryRecord,
    *,
    update_thresh: float = 0.62,
    noop_thresh: float = 0.97,
    resolve_conflicts: bool = False,
) -> RouteDecision:
    """Mem0-style write router: is a candidate memory genuinely new (ADD), a variant
    of one we already hold (UPDATE — reinforce + merge), already fully captured (NOOP),
    or a CONTRADICTION of a prior we must retire (SUPERSEDE)? Compared only within the
    same tier; the earliest best match wins ties.

    With ``resolve_conflicts`` the router goes *beyond* similar→reinforce: when the
    candidate is about the same entity as a prior (same asset / high similarity) but
    names a DIFFERENT diagnosed root cause, the fact has changed — the prior is stale.
    That returns SUPERSEDE (retire the old, promote the new) instead of UPDATE (which
    would merge the stale root into the live memory and keep contradicting itself).
    """
    best: MemoryRecord | None = None
    best_sim = 0.0
    for rec in memory.active():
        if rec.tier != candidate.tier or rec.memory_id == candidate.memory_id:
            continue
        s = similarity(candidate.tags, candidate.asset_ids, rec.tags, rec.asset_ids)
        if s > best_sim:
            best, best_sim = rec, s
    if best is None or best_sim < update_thresh:
        return RouteDecision("ADD", None, best_sim)
    if resolve_conflicts:
        cand_roots, targ_roots = _root_keys(candidate.tags), _root_keys(best.tags)
        # same entity (they matched at update_thresh), but the candidate diagnoses a root
        # the prior does not — a genuine contradiction, not a reinforcing variant. Checked
        # BEFORE the NOOP gate: root:* tags are excluded from the content-similarity blend,
        # so a pure root flip can otherwise look like an identical re-observation.
        if cand_roots and targ_roots and (cand_roots - targ_roots):
            return RouteDecision("SUPERSEDE", best.memory_id, best_sim)
    new_info = bool((_tagset(candidate.tags) - _tagset(best.tags)) or (set(candidate.asset_ids) - set(best.asset_ids)))
    if best_sim >= noop_thresh and not new_info:
        return RouteDecision("NOOP", best.memory_id, best_sim)
    return RouteDecision("UPDATE", best.memory_id, best_sim)


def supersede(
    memory: TieredMemoryStore,
    old_id: str,
    candidate: MemoryRecord,
    *,
    recorder: list[dict] | None = None,
) -> str:
    """Conflict-resolving UPDATE: ``candidate`` contradicts the prior ``old_id`` on the
    same entity, so the prior is now stale. Retire the old (quarantine reason
    ``superseded``), promote the new as the live memory, and record the supersession
    both ways (old.superseded_by / new links to old) for an auditable provenance chain.
    Returns the id of the live memory. Idempotent-safe: a missing/duplicate id degrades
    to a plain ADD rather than raising."""
    old = memory.get(old_id)
    if memory.get(candidate.memory_id) is None:
        candidate.strength = 1.0
        memory.add(candidate)
    if old is None or old.quarantined:
        return candidate.memory_id
    before = _snap(old)
    if old_id not in candidate.links:
        candidate.links.append(old_id)      # provenance: what this memory replaced
    old.superseded_by = candidate.memory_id
    memory.quarantine(old_id, "superseded")
    # quarantine flips a flag _snap() does not track, so emit the op explicitly rather
    # than via _emit_change (whose before==after guard would swallow it).
    _emit(recorder, "SUPERSEDE", old_id, old.tier, target_id=candidate.memory_id,
          before=before, after=_snap(old))
    return candidate.memory_id


def apply_route(memory: TieredMemoryStore, candidate: MemoryRecord, decision: RouteDecision, *, conf_cap: float = 3.0, recorder: list[dict] | None = None) -> str:
    """Execute a RouteDecision; returns the memory_id that now holds the information."""
    if decision.op == "ADD":
        candidate.strength = 1.0
        memory.add(candidate)
        return candidate.memory_id
    if decision.op == "SUPERSEDE" and decision.target_id:
        return supersede(memory, decision.target_id, candidate, recorder=recorder)
    target = memory.get(decision.target_id) if decision.target_id else None
    if target is None:                       # target vanished (forgotten) — treat as ADD
        candidate.strength = 1.0
        memory.add(candidate)
        return candidate.memory_id
    target.strength = 1.0                     # any hit refreshes retrievability
    target.access_count += 1                  # reuse: feeds utility-driven eviction
    if decision.op == "UPDATE":
        retrieval_changed = False
        for t in candidate.tags:
            if t not in target.tags:
                target.tags.append(t)
                retrieval_changed = True
        for a in candidate.asset_ids:
            if a not in target.asset_ids:
                target.asset_ids.append(a)
                retrieval_changed = True
        target.confidence = min(conf_cap, target.confidence + 0.3)
        target.importance += 1.0
        target.source_trace_ids.extend(t for t in candidate.source_trace_ids if t not in target.source_trace_ids)
        if candidate.evidence_snapshot and not target.evidence_snapshot:
            target.evidence_snapshot = candidate.evidence_snapshot
        if retrieval_changed:
            memory.reindex(target.memory_id)
    return target.memory_id


def link_related(
    memory: TieredMemoryStore,
    record: MemoryRecord,
    *,
    k: int = 3,
    thresh: float = 0.34,
    recorder: list[dict] | None = None,
) -> list[str]:
    """A-MEM: connect a memory bidirectionally to its k most similar neighbours, so
    recall can traverse family links from a weak hit to a strong one.

    `recorder` is write-only observability (see observatory.emit): the link is
    bidirectional, so BOTH sides are mutated and both therefore get their own LINK
    event with a genuine before/after. The neighbour's mutation used to be invisible.
    """
    scored: list[tuple[float, MemoryRecord]] = []
    for rec in memory.active():
        if rec.memory_id == record.memory_id:
            continue
        s = similarity(record.tags, record.asset_ids, rec.tags, rec.asset_ids)
        if s >= thresh:
            scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    linked: list[str] = []
    for _, rec in scored[:k]:
        before_self, before_other = _snap(record), _snap(rec)
        if rec.memory_id not in record.links:
            record.links.append(rec.memory_id)
        if record.memory_id not in rec.links:
            rec.links.append(record.memory_id)
        # the A-MEM neighbour score IS real here, unlike the Mem0 route score.
        _emit_change(recorder, "LINK", record, before_self, similarity=s, target_id=rec.memory_id)
        _emit_change(recorder, "LINK", rec, before_other, similarity=s, target_id=record.memory_id)
        linked.append(rec.memory_id)
    return linked


def neighbours(memory: TieredMemoryStore, record: MemoryRecord) -> list[MemoryRecord]:
    """Resolve a record's A-MEM links to live (non-quarantined) records."""
    return [n for mid in record.links if (n := memory.get(mid)) is not None and not n.quarantined]


def reflect(
    memory: TieredMemoryStore,
    *,
    importance_thresh: float = 3.0,
    min_members: int = 2,
    recorder: list[dict] | None = None,
) -> list[str]:
    """Generative-Agents reflection: when an incident family (grouped by primary
    asset) has accrued enough salience across >= min_members episodic incidents,
    synthesize a higher-level semantic *insight* that names the family, records its
    distinct root causes, and links its members. Idempotent per family; an existing
    insight is refreshed instead of duplicated. Returns newly created insight ids.

    `recorder` is write-only observability (see observatory.emit). The RETURN VALUE
    only ever names *newly created* insights, so a caller watching it cannot see the
    refresh branch below — which re-derives importance from the family's current
    salience on EVERY pass. Those mutations are emitted here, at the mutation site,
    as INSIGHT_REFRESH; recording them is what keeps the event stream reconcilable
    with the final store."""
    families: dict[str, list[MemoryRecord]] = {}
    for rec in memory.active():
        if rec.tier == "episodic" and rec.asset_ids:
            families.setdefault(rec.asset_ids[0], []).append(rec)

    created: list[str] = []
    for fam, members in families.items():
        if len(members) < min_members:
            continue
        salience = sum(m.importance + m.confidence for m in members)
        if salience < importance_thresh:
            continue
        insight_id = f"insight-{fam}"
        existing = memory.get(insight_id)
        roots = sorted({t[len("root:"):] for m in members for t in m.tags if t.startswith("root:")})
        member_ids = [m.memory_id for m in members]
        if existing is not None:                       # keep an existing insight current
            before = _snap(existing)
            retrieval_changed = False
            existing.strength = 1.0
            existing.importance = salience
            for mid in member_ids:
                if mid not in existing.links:
                    existing.links.append(mid)
            for r in roots:
                if r not in existing.tags:
                    existing.tags.append(r)
                    retrieval_changed = True
            if retrieval_changed:
                memory.reindex(existing.memory_id)
            # NOT a REINFORCE: that op is a fixed += increment from a recall hit. This
            # re-derives importance ABSOLUTELY from the family's salience, so it can
            # move either way and routinely overwrites a REINFORCE increment. No
            # route() ran on this dedupe-by-id path, so there is no similarity.
            _emit_change(recorder, "INSIGHT_REFRESH", existing, before,
                         source_memory_ids=list(member_ids))
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
                before_m = _snap(m)
                m.links.append(insight_id)
                # the member is an EXISTING record being mutated by reflection; the
                # INSIGHT event only describes the insight, so record this side too.
                _emit_change(recorder, "LINK", m, before_m, target_id=insight_id)
        created.append(insight_id)
    return created


def decay_and_forget(memory: TieredMemoryStore, *, retention: float = 0.55, floor: float = 0.4, protect: tuple[str, ...] = ("seed", "asset", "insight")) -> list[str]:
    """Ebbinghaus tick: every non-protected active memory loses retrievability;
    those below the floor are forgotten (quarantined). Memories reused this tick were
    reset to strength 1.0 and survive; a memory unused for ~2 ticks fades out.
    Protected priors (seeded / asset-profile / reflected insights) never decay.
    Returns the ids forgotten this tick."""
    if not 0.0 < retention <= 1.0:
        raise ValueError(f"retention must be in (0, 1], got {retention}")
    if floor < 0.0:
        raise ValueError(f"floor must be >= 0, got {floor}")
    forgotten: list[str] = []
    for rec in memory.active():
        if rec.memory_id.startswith(protect):
            continue
        rec.strength *= retention
        if rec.strength < floor:
            memory.quarantine(rec.memory_id, "forgotten")
            forgotten.append(rec.memory_id)
    return forgotten


@dataclass(frozen=True)
class UtilityWeights:
    """Blend weights for the per-memory utility used by eviction. Equal by default —
    deliberately *not* tuned on any eval label, so the eviction is honest."""
    importance: float = 0.25   # Generative-Agents salience (reflection)
    access: float = 0.25       # recall frequency — how often it has been reused
    recency: float = 0.25      # Ebbinghaus retrievability (strength), reset on reuse
    centrality: float = 0.25   # A-MEM link degree — how many families it bridges


_EVICT_PROTECT = ("seed", "asset", "insight")


def utility_scores(
    memory: TieredMemoryStore,
    *,
    weights: UtilityWeights = UtilityWeights(),
    protect: tuple[str, ...] = _EVICT_PROTECT,
) -> dict[str, float]:
    """Per-memory utility in [0,1]: a max-normalised blend of importance, access
    frequency, recency (strength) and A-MEM centrality (link degree). This is a real
    function of four independent lifecycle signals — NOT a relabelled time-decay: two
    memories of equal age get different utility if one is more reused, more salient, or
    more central. Protected priors are omitted (never evicted, so never scored)."""
    active = [r for r in memory.active() if not r.memory_id.startswith(protect)]
    if not active:
        return {}

    def _norm(values: dict[str, float]) -> dict[str, float]:
        hi = max(values.values(), default=0.0)
        return {k: (v / hi if hi > 0 else 0.0) for k, v in values.items()}

    imp = _norm({r.memory_id: max(0.0, r.importance) for r in active})
    acc = _norm({r.memory_id: float(r.access_count) for r in active})
    rec = _norm({r.memory_id: max(0.0, r.strength) for r in active})
    cen = _norm({r.memory_id: float(len(r.links)) for r in active})
    w = weights
    return {
        r.memory_id: round(
            w.importance * imp[r.memory_id] + w.access * acc[r.memory_id]
            + w.recency * rec[r.memory_id] + w.centrality * cen[r.memory_id],
            6,
        )
        for r in active
    }


def utility_evict(
    memory: TieredMemoryStore,
    *,
    budget: int,
    weights: UtilityWeights = UtilityWeights(),
    protect: tuple[str, ...] = _EVICT_PROTECT,
    recorder: list[dict] | None = None,
) -> list[str]:
    """Capacity-budgeted eviction: while the active store exceeds ``budget``, forget the
    LOWEST-utility non-protected memories (quarantine reason ``evicted``). Protected
    priors (seeded / asset-profile / reflected insights) are never evicted and always
    count against the budget. Returns the ids evicted this call.

    This is the wired, non-trivial counterpart to :func:`decay_and_forget`: eviction is
    driven by *learned worth* (utility), not by age alone. Budget binds only when the
    store has grown past it — on a store that fits, this is a no-op."""
    if budget < 0:
        raise ValueError(f"budget must be >= 0, got {budget}")
    active = memory.active()
    if len(active) <= budget:
        return []
    protected = [r for r in active if r.memory_id.startswith(protect)]
    evictable = [r for r in active if not r.memory_id.startswith(protect)]
    keep = max(0, budget - len(protected))              # protected priors count vs budget
    scores = utility_scores(memory, weights=weights, protect=protect)
    # lowest utility first; ties broken by lower strength then insertion order (stable).
    order = sorted(evictable, key=lambda r: (scores.get(r.memory_id, 0.0), r.strength))
    to_evict = order[: max(0, len(evictable) - keep)]
    forgotten: list[str] = []
    for rec in to_evict:
        before = _snap(rec)
        memory.quarantine(rec.memory_id, "evicted")
        _emit(recorder, "EVICT", rec.memory_id, rec.tier, before=before, after=_snap(rec),
              similarity=scores.get(rec.memory_id, 0.0))
        forgotten.append(rec.memory_id)
    return forgotten


def memory_health(memory: TieredMemoryStore) -> dict[str, object]:
    """A compact, honest snapshot of store health for measurement / the UI."""
    active = memory.active()
    by_tier: dict[str, int] = {}
    for r in active:
        by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
    return {
        "active": len(active),
        "forgotten": sum(1 for r in memory.records() if r.quarantined),
        "insights": sum(1 for r in active if "insight" in r.tags),
        "links": sum(len(r.links) for r in active),
        "by_tier": by_tier,
        "index": memory.index_health(),
    }
