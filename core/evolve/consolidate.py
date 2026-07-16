"""Offline self-evolution: turn a completed trajectory into durable learning.

This is the "background re-evolution" half of the architecture — it consumes the
trace ledger of one run and writes back into the persistent core:

  * episodic  — this specific incident (query → root cause, cited evidence)
  * semantic  — the recurring pattern (root-cause key), confidence grows on reuse
  * procedural— for this pattern, the skills that actually mattered (the shortcut
                the online path reuses next time to probe less)
  * skills    — success/misuse counts updated; consistently useless skills frozen
  * memory    — records that contributed to a verified answer are reinforced;
                records cited by a rejected answer are quarantined

Nothing here is synthesized: every field is derived from the real run events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from core.evolve.memory_ops import apply_route, link_related, reflect, route
from core.evolve.observatory import added as _added
from core.evolve.observatory import snapshot as _snap
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent


class CaseLike(Protocol):
    """The case attributes consolidation needs (structural; any domain case fits)."""

    id: str
    query: str
    query_terms: list[str]
    assets: list[str]


@dataclass
class ConsolidationReport:
    """What one consolidation pass changed, by memory/skill id — the audit trail
    for every write the offline loop makes."""
    run_id: str
    passed: bool
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    reinforced: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)
    skills_success: list[str] = field(default_factory=list)
    skills_misuse: list[str] = field(default_factory=list)
    skills_frozen: list[str] = field(default_factory=list)


def _first(events: list[TraceEvent], kind: str) -> TraceEvent | None:
    for event in events:
        if event.kind == kind:
            return event
    return None


def _emit(
    recorder: list[dict] | None,
    op: str,
    memory_id: str,
    tier: str | None,
    *,
    similarity: float | None = None,
    target_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    source_memory_ids: list[str] | None = None,
) -> None:
    """Append one lifecycle op to an optional observability recorder.

    Purely a side-channel: when `recorder` is None (every existing caller) this is a
    no-op, and it never influences the decision it is describing. `similarity` is the
    REAL RouteDecision score where a route ran, and None where no routing happened.
    """
    if recorder is None:
        return
    recorder.append({
        "op": op,
        "memory_id": memory_id,
        "tier": tier,
        "similarity": similarity,
        "target_id": target_id,
        "before": before,
        "after": after,
        "added_tags": _added(before, after, "tags"),
        "added_assets": _added(before, after, "asset_ids"),
        "source_memory_ids": list(source_memory_ids or []),
    })


def consolidate_run(
    events: list[TraceEvent],
    case: CaseLike,
    memory: TieredMemoryStore,
    skills: SkillRegistry,
    evidence: list[dict] | None = None,
    *,
    freeze_after: int = 4,
    misuse_thresh: float = 0.5,
    conf_cap: float = 3.0,
    recorder: list[dict] | None = None,
) -> ConsolidationReport:
    """Consume one run's trace and write durable learning back into the store.

    Verified runs add/reinforce episodic + semantic + procedural memories and
    credit the skills whose evidence the verdict cited; rejected runs quarantine
    the memories they leaned on. Pure trace-derived — nothing is synthesized.
    Returns the audit report of every id touched.

    Pass an optional `recorder` list to also collect the item-level lifecycle ops
    (ADD/UPDATE/NOOP/REINFORCE/QUARANTINE/INSIGHT/LINK with real before/after
    snapshots). It is write-only observability: it cannot alter any decision here,
    and omitting it leaves behaviour byte-identical.
    """
    diag = _first(events, "diagnosis_completed")
    verif = _first(events, "verifier_result")
    mem_read = _first(events, "memory_read")
    exposed_ev = _first(events, "skills_exposed")
    run_id = events[0].run_id if events else ""
    report = ConsolidationReport(run_id=run_id, passed=False)
    if diag is None:
        return report

    passed = bool(verif and verif.payload.get("passed"))
    report.passed = passed
    root_key = str(diag.payload.get("root_cause_key", ""))
    cited = {e.get("evidence_id") for e in diag.payload.get("evidence", [])}
    confidence = float(diag.payload.get("confidence", 0.0))
    exposed = list(exposed_ev.payload.get("skills", [])) if exposed_ev else []
    resolved_from_memory = _first(events, "memory_resolved") is not None
    terms = [t.lower() for t in case.query_terms]
    # root_key carried as a distinguishable tag so reflection can recover a family's
    # distinct root causes regardless of later tag merges.
    tags_base = [*terms, root_key, f"root:{root_key}"]
    snapshot = [dict(e) for e in (evidence or []) if e.get("evidence_id") in cited]

    # which exposed skills actually produced the evidence the verdict cited?
    winning: list[str] = []
    produced: dict[str, int] = {}
    for event in events:
        if event.kind == "tool_called" and not event.payload.get("blocked"):
            name = event.payload.get("skill")
            if not name:
                continue
            evids = set(event.payload.get("evidence_ids", []))
            produced[name] = produced.get(name, 0) + len(evids)
            if (evids & cited) and name not in winning:
                winning.append(name)

    if passed and root_key:
        # episodic: the concrete incident (with a provenance-linked evidence snapshot so
        # a future recurrence can be resolved by recall). Skip if this run was ITSELF a
        # recall — no point storing a duplicate of what we just remembered.
        if not resolved_from_memory:
            epi = MemoryRecord(
                memory_id=f"epi-{case.id}-{uuid4().hex[:6]}", tier="episodic",
                text=f"{case.id}: {case.query[:80]} -> {root_key}",
                tags=tags_base, asset_ids=list(case.assets), evidence_ids=sorted(x for x in cited if x),
                confidence=max(0.8, confidence), source_trace_ids=[run_id],
                evidence_snapshot=snapshot,
            )
            # Mem0 write router: a genuinely new incident is ADDed (and linked into its
            # family, A-MEM); a re-observed variant UPDATEs the prior instead of duplicating.
            decision = route(memory, epi)
            # observability only: read the target's state either side of the in-place
            # merge, so the UI can show the REAL diff apply_route would otherwise lose.
            target = memory.get(decision.target_id) if decision.target_id else None
            before = _snap(target) if target is not None else None
            held_id = apply_route(memory, epi, decision)
            held = memory.get(held_id)
            _emit(
                recorder, decision.op, held_id, held.tier if held is not None else epi.tier,
                similarity=decision.similarity, target_id=decision.target_id,
                before=before, after=_snap(held) if held is not None else None,
            )
            if decision.op == "ADD":
                report.added.append(held_id)
                linked = link_related(memory, epi)
                report.linked.extend(linked)
                for neighbour_id in linked:
                    _emit(recorder, "LINK", held_id, epi.tier, target_id=neighbour_id)
            elif decision.op == "UPDATE":
                report.updated.append(held_id)
            else:
                report.reinforced.append(held_id)

        # semantic: the recurring pattern (dedupe by root cause), reinforced on reuse
        sem_id = f"sem-{root_key}"
        sem = memory.get(sem_id)
        if sem is not None:
            before = _snap(sem)
            sem.confidence = min(conf_cap, sem.confidence + 0.3)
            sem.importance += 1.0
            sem.strength = 1.0
            report.reinforced.append(sem_id)
            _emit(recorder, "REINFORCE", sem_id, sem.tier, before=before, after=_snap(sem))
        else:
            sem_rec = MemoryRecord(
                memory_id=sem_id, tier="semantic", text=f"pattern: {root_key}",
                tags=tags_base, asset_ids=list(case.assets), confidence=1.2, source_trace_ids=[run_id],
            )
            memory.add(sem_rec)
            report.added.append(sem_id)
            # no route() runs on this dedupe-by-id path, so there is no similarity to report.
            _emit(recorder, "ADD", sem_id, "semantic", after=_snap(sem_rec))

        # procedural: for this pattern, the skills that mattered (the online shortcut)
        proc_id = f"proc-{root_key}"
        skill_tags = [f"skill:{s}" for s in winning]
        proc = memory.get(proc_id)
        if proc is not None:
            before = _snap(proc)
            proc.confidence = min(conf_cap, proc.confidence + 0.4)
            proc.importance += 1.0
            proc.strength = 1.0
            for st in skill_tags:
                if st not in proc.tags:
                    proc.tags.append(st)
            report.reinforced.append(proc_id)
            _emit(recorder, "REINFORCE", proc_id, proc.tier, before=before, after=_snap(proc))
        elif winning:
            proc_rec = MemoryRecord(
                memory_id=proc_id, tier="procedural",
                text=f"for {root_key}, probe {', '.join(winning)}",
                tags=[*tags_base, *skill_tags], asset_ids=list(case.assets),
                confidence=1.5, source_trace_ids=[run_id],
            )
            memory.add(proc_rec)
            report.added.append(proc_id)
            _emit(recorder, "ADD", proc_id, "procedural", after=_snap(proc_rec))

        # reinforce the retrieved memories that contributed to a verified answer
        if mem_read:
            for ids in mem_read.payload.values():
                for mid in ids:
                    rec = memory.get(mid)
                    if rec is not None and rec.memory_id not in report.added:
                        before = _snap(rec)
                        rec.confidence = min(conf_cap, rec.confidence + 0.1)
                        rec.importance += 0.5
                        rec.strength = 1.0     # reuse refreshes retrievability (vs. Ebbinghaus decay)
                        report.reinforced.append(mid)
                        _emit(recorder, "REINFORCE", mid, rec.tier, before=before, after=_snap(rec))
    else:
        # a rejected answer: distrust the memories it leaned on
        if mem_read:
            for ids in mem_read.payload.values():
                for mid in ids:
                    rec = memory.get(mid)
                    before = _snap(rec) if rec is not None else None
                    memory.quarantine(mid, "contradicted")
                    report.quarantined.append(mid)
                    _emit(
                        recorder, "QUARANTINE", mid, rec.tier if rec is not None else None,
                        before=before, after=_snap(rec) if rec is not None else None,
                    )

    # skill evolution: reward what worked, penalise the truly useless, prune the persistently bad
    for skill in skills.all():
        name = skill.spec.name
        if passed and name in winning:
            skill.spec.success_count += 1
            report.skills_success.append(name)
        elif name in exposed and produced.get(name, 0) == 0:
            skill.spec.misuse_count += 1
            report.skills_misuse.append(name)
        attempts = skill.spec.success_count + skill.spec.misuse_count
        if attempts >= freeze_after and not skill.spec.frozen and skill.spec.misuse_count / attempts > misuse_thresh:
            skill.spec.frozen = True
            report.skills_frozen.append(name)

    # Generative-Agents reflection: once a family has matured, abstract it into a
    # higher-level insight that links its members (idempotent; safe to call each run).
    report.insights = reflect(memory)
    for insight_id in report.insights:
        insight = memory.get(insight_id)
        if insight is not None:
            # reflect() writes the family's member ids straight onto links — that IS
            # the real reflection provenance, not a reconstruction.
            _emit(
                recorder, "INSIGHT", insight_id, insight.tier,
                after=_snap(insight), source_memory_ids=list(insight.links),
            )
    return report
