"""Read-only observability for the memory lifecycle.

The self-evolution loop already makes every write decision explicitly (Mem0 route,
A-MEM links, reflection, quarantine) — but it collapses them into id-lists and then
throws the store away. This module *only serializes what already happened*:

  * it never decides anything, never mutates a record, never changes control flow;
  * every number it emits was produced by the real kernel on the real run;
  * where the kernel genuinely does not expose a value (per-link and retrieval
    scores) it emits ``None`` rather than inventing one.

``CAPABILITIES`` states, honestly, which lifecycle signals the kernel currently
produces — so the UI can grey unsupported affordances out instead of faking them.
"""
from __future__ import annotations

from typing import Any

from core.memory.store import MemoryRecord, TieredMemoryStore
from core.trace.events import TraceEvent

_QUARANTINE_PREFIX = "quarantine:"

# evidence_snapshot bodies can carry the full observed record; the UI only needs
# provenance + a human-readable gist, so we trim fields (never invent them).
_MAX_SUMMARY_CHARS = 240

# A fact about the code, not a toggle: does the kernel actually run this lifecycle path?
#   decay_wired          — whether decay_and_forget() itself runs in the production loop.
#                          It does not. The loop uses utility_evict(), whose score includes
#                          recency but is not equivalent to time-decay forgetting.
#   eviction_wired       — capacity-budgeted UTILITY eviction (utility_evict) is the wired
#                          path — worth (importance+access+recency+centrality), not age
#                          alone, decides what is forgotten.
#   conflict_update_wired— route(resolve_conflicts=True) resolves contradictions: a memory
#                          that renames the root cause on the same entity SUPERSEDEs the
#                          stale prior instead of merging into it. Emits SUPERSEDE ops.
#   retrieval_scores     — TieredMemoryStore.retrieve() computes a score but returns
#                          only records; the score never reaches the trace.
#   context_drop_reason  — ContextCompiler records section, reason, text fragment,
#                          identity, and whether a drop was a partial truncation.
#   update_text_mutation — apply_route()'s UPDATE merges tags/assets/confidence but
#                          never rewrites target.text, so there is no text diff.
CAPABILITIES: dict[str, bool] = {
    "decay_wired": False,
    "eviction_wired": True,
    "conflict_update_wired": True,
    "retrieval_scores": False,
    "context_drop_reason": True,
    "update_text_mutation": False,
}


def _first(events: list[TraceEvent], kind: str) -> TraceEvent | None:
    for event in events:
        if event.kind == kind:
            return event
    return None


def snapshot(record: MemoryRecord) -> dict[str, Any]:
    """The mutable fields of a record, deep-copied.

    apply_route/reinforcement mutate lists in place by ``append``, so the lists MUST
    be copied here or a 'before' snapshot would alias — and silently equal — 'after'.
    """
    return {
        "confidence": record.confidence,
        "importance": record.importance,
        "strength": record.strength,
        "tags": list(record.tags),
        "asset_ids": list(record.asset_ids),
        "links": list(record.links),
    }


def added(before: dict[str, Any] | None, after: dict[str, Any] | None, key: str) -> list[str]:
    """Real set difference after-minus-before, in after's order.

    With no ``before`` (e.g. ADD) there is no diff to take — returns [], rather than
    misreporting a brand-new record's whole tag list as 'added by this op'.
    """
    if before is None or after is None:
        return []
    seen = set(before.get(key, []))
    return [x for x in after.get(key, []) if x not in seen]


def emit(
    recorder: list[dict] | None,
    op: str,
    memory_id: str,
    tier: str | None,
    *,
    similarity: float | None = None,
    target_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    source_memory_ids: list[str] | None = None,
) -> None:
    """Append one lifecycle op to an optional observability recorder.

    Purely a side-channel: when `recorder` is None (the kernel's own callers) this is
    a no-op, and it never influences the decision it is describing. `similarity` is
    the REAL RouteDecision score where a route ran, and None where no routing
    happened — it is never invented for paths route() never touched.

    Lives here rather than in consolidate.py because the mutation sites span both
    consolidate.py and memory_ops.py, and memory_ops must not import consolidate.
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
        "added_tags": added(before, after, "tags"),
        "added_assets": added(before, after, "asset_ids"),
        "source_memory_ids": list(source_memory_ids or []),
    })


def quarantine_reason(record: MemoryRecord) -> str | None:
    """The reason a record was quarantined, parsed from its ``quarantine:<reason>`` tag."""
    if not record.quarantined:
        return None
    for tag in reversed(record.tags):
        if tag.startswith(_QUARANTINE_PREFIX):
            return tag[len(_QUARANTINE_PREFIX):]
    return None


def _trim_evidence(snapshot_items: list[dict]) -> list[dict]:
    """Keep provenance + a readable gist of each observed evidence item; drop bulk."""
    trimmed: list[dict] = []
    for item in snapshot_items:
        summary = str(item.get("summary", ""))
        trimmed.append({
            "evidence_id": item.get("evidence_id"),
            "source": item.get("source"),
            "summary": summary[:_MAX_SUMMARY_CHARS],
            "truncated": len(summary) > _MAX_SUMMARY_CHARS,
        })
    return trimmed


def serialize_record(record: MemoryRecord) -> dict[str, Any]:
    """One memory record as the UI sees it — every field straight off the record."""
    return {
        "memory_id": record.memory_id,
        "tier": record.tier,
        "text": record.text,
        "tags": list(record.tags),
        "asset_ids": list(record.asset_ids),
        "evidence_ids": list(record.evidence_ids),
        "confidence": record.confidence,
        "importance": record.importance,
        "strength": record.strength,
        "quarantined": record.quarantined,
        "quarantine_reason": quarantine_reason(record),
        "source_trace_ids": list(record.source_trace_ids),
        "links": list(record.links),
        "evidence_snapshot": _trim_evidence(record.evidence_snapshot),
    }


def serialize_store(memory: TieredMemoryStore) -> list[dict[str, Any]]:
    """The FINAL warm store: active AND quarantined records (``records()``, not ``active()``)."""
    return [serialize_record(record) for record in memory.records()]


def recall_row(
    events: list[TraceEvent],
    *,
    seq: int,
    pass_no: int,
    case_id: str,
    run_id: str,
    probes: int,
) -> dict[str, Any]:
    """What one run retrieved, what survived into context, and what was dropped.

    ``dropped_memory_ids`` is a real derivation (retrieved minus included), not a
    guess. ``context_drops`` carries the compiler's section-local provenance,
    including partial truncations of items that also appear in the included list.
    """
    mem_read = _first(events, "memory_read")
    context = _first(events, "context_compiled")
    resolved_ev = _first(events, "memory_resolved")

    retrieved = {tier: list(ids) for tier, ids in mem_read.payload.items()} if mem_read else {}
    included = list(context.payload.get("included_memory_ids", [])) if context else []
    included_set = set(included)
    dropped = [mid for ids in retrieved.values() for mid in ids if mid not in included_set]
    memory_kinds = {"asset_profile", "semantic", "procedural", "episodic"}
    context_drops = []
    if context is not None:
        for section in context.payload.get("sections", []):
            for item in section.get("dropped", []):
                if item.get("kind") in memory_kinds:
                    context_drops.append({"section": section.get("name"), **item})

    resolved_ids: list[str] = []
    if resolved_ev is not None and resolved_ev.payload.get("memory_id"):
        resolved_ids = [str(resolved_ev.payload["memory_id"])]

    return {
        "seq": seq,
        "pass": pass_no,
        "case_id": case_id,
        "run_id": run_id,
        "retrieved": retrieved,
        "included_memory_ids": included,
        "dropped_memory_ids": dropped,
        "context_drops": context_drops,
        "probes": probes,
        "shortcut": any(e.kind == "memory_shortcut" for e in events),
        "resolved": resolved_ev is not None,
        "resolved_memory_ids": resolved_ids,
    }
