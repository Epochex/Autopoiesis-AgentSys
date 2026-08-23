"""Read-only observability for the memory lifecycle.

The self-evolution loop already makes every write decision explicitly (Mem0 route,
A-MEM links, reflection, quarantine) — but it collapses them into id-lists and then
throws the store away. This module *only serializes what already happened*:

  * it never decides anything, never mutates a record, never changes control flow;
  * every number it emits was produced by the real kernel on the real run;
  * where the kernel genuinely does not expose a value it emits ``None`` rather
    than inventing one.

``CAPABILITIES`` describes production wiring. A stream response separately reports
whether each mechanism exists, whether that benchmark run configured it, and whether
it actually fired; those facts must not be collapsed into one boolean.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.memory.store import MemoryRecord, TieredMemoryStore
from core.trace.events import TraceEvent

_QUARANTINE_PREFIX = "quarantine:"

# evidence_snapshot bodies can carry the full observed record; the UI only needs
# provenance + a human-readable gist, so we trim fields (never invent them).
_MAX_SUMMARY_CHARS = 240

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CapabilityEvidence:
    """One statically checkable fact required for a production wiring claim."""

    path: str
    kind: Literal["call", "dict_key"]
    name: str
    marker: str | None = None
    keyword: str | None = None
    keyword_value: bool | None = None


# Each inner tuple is one complete proof; alternatives are ORed. Every item inside
# a proof must be present. These locators are maintained when a production entry
# point moves. The acceptance criterion is executable: the named production file
# must contain the specified call or emitted dictionary key. Tests, benchmarks and
# evaluation drivers are deliberately absent.
PRODUCTION_CALL_SITES: dict[str, tuple[tuple[CapabilityEvidence, ...], ...]] = {
    "decay": ((CapabilityEvidence(
        "core/orchestrator/evolving_service.py", "call", "decay_and_forget"
    ),),),
    "eviction": ((CapabilityEvidence(
        "core/orchestrator/evolving_service.py", "call", "utility_evict"
    ),),),
    "contradiction_quarantine": (
        (CapabilityEvidence(
            "domains/network_rca/factory.py", "dict_key", "contradicts"
        ),),
        (CapabilityEvidence(
            "frontend/gateway/app/sentinel_wiring.py", "dict_key", "contradicts"
        ),),
    ),
    "conflict_update": (
        (CapabilityEvidence(
            "domains/network_rca/incident_memory.py",
            "call",
            "consolidate_run",
            keyword="resolve_conflicts",
            keyword_value=True,
        ),),
        (CapabilityEvidence(
            "domains/network_rca/factory.py",
            "call",
            "build_network_rca_service",
            keyword="resolve_conflicts",
            keyword_value=True,
        ),),
        (CapabilityEvidence(
            "frontend/gateway/app/sentinel_wiring.py",
            "call",
            "consolidate_incident_timeline",
            keyword="resolve_conflicts",
            keyword_value=True,
        ),),
        (CapabilityEvidence(
            "core/orchestrator/evolving_service.py",
            "call",
            "consolidate_run",
            keyword="resolve_conflicts",
            keyword_value=True,
        ),),
    ),
    "retrieval_scoring": ((CapabilityEvidence(
        "core/orchestrator/orchestrator.py",
        "call",
        "_record",
        marker="memory_candidates_ranked",
    ),),),
    "context_drop_provenance": ((
        CapabilityEvidence(
            "core/context/compiler.py", "call", "_provenance", keyword="reason"
        ),
        CapabilityEvidence(
            "core/orchestrator/orchestrator.py",
            "call",
            "_record",
            marker="context_compiled",
        ),
    ),),
    "update_text_mutation": (),
}


def _call_name(call: ast.Call) -> str:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _literal_strings(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _evidence_present(evidence: CapabilityEvidence) -> bool:
    path = _REPOSITORY_ROOT / evidence.path
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        # Missing or temporarily invalid source cannot support a capability claim.
        return False
    if evidence.kind == "dict_key":
        return any(
            isinstance(node, ast.Dict)
            and evidence.name in {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            for node in ast.walk(tree)
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != evidence.name:
            continue
        if evidence.marker is not None and evidence.marker not in _literal_strings(node):
            continue
        if evidence.keyword is not None:
            keyword = next(
                (item for item in node.keywords if item.arg == evidence.keyword), None
            )
            if keyword is None:
                continue
            if evidence.keyword_value is not None and not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value is evidence.keyword_value
            ):
                continue
        return True
    return False


def _production_wired(capability: str) -> bool:
    return any(
        all(_evidence_present(evidence) for evidence in proof)
        for proof in PRODUCTION_CALL_SITES[capability]
    )


def _defines_function(path: str, name: str) -> bool:
    try:
        tree = ast.parse((_REPOSITORY_ROOT / path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


# Implementation availability is derived from concrete function definitions where
# there is a named mechanism. The two observability capabilities use their complete
# production evidence proofs because their implementation is the emitted payload.
# ``update_text_mutation`` is the one manually maintained negative fact: it may turn
# True only when apply_route() rewrites target.text and the lifecycle event exposes a
# before/after text diff. Absence cannot be proved by the presence of a call site.
_IMPLEMENTED_MECHANISMS: dict[str, bool] = {
    "decay": _defines_function("core/evolve/memory_ops.py", "decay_and_forget"),
    "eviction": _defines_function("core/evolve/memory_ops.py", "utility_evict"),
    "contradiction_quarantine": _defines_function(
        "core/evolve/consolidate.py", "_fresh_cited_contradictions"
    ),
    "conflict_update": (
        _defines_function("core/evolve/memory_ops.py", "route")
        and _defines_function("core/evolve/memory_ops.py", "supersede")
    ),
    "retrieval_scoring": _production_wired("retrieval_scoring"),
    "context_drop_provenance": _production_wired("context_drop_provenance"),
    "update_text_mutation": False,
}


CAPABILITY_STATUS: dict[str, dict[str, bool]] = {
    name: {
        "implemented": implemented,
        "production_wired": _production_wired(name),
    }
    for name, implemented in _IMPLEMENTED_MECHANISMS.items()
}


# Compatibility view for existing API consumers. No truth value lives here: every
# entry is projected from the structured implementation/wiring status above.
CAPABILITIES: dict[str, bool] = {
    "decay_wired": CAPABILITY_STATUS["decay"]["production_wired"],
    "eviction_wired": CAPABILITY_STATUS["eviction"]["production_wired"],
    "contradiction_quarantine_wired": CAPABILITY_STATUS[
        "contradiction_quarantine"
    ]["production_wired"],
    "conflict_update_wired": CAPABILITY_STATUS["conflict_update"]["production_wired"],
    "retrieval_scores": CAPABILITY_STATUS["retrieval_scoring"]["production_wired"],
    "context_drop_reason": CAPABILITY_STATUS[
        "context_drop_provenance"
    ]["production_wired"],
    "update_text_mutation": CAPABILITY_STATUS["update_text_mutation"]["implemented"],
}


def runtime_capability_status(
    events: list[dict[str, Any]],
    recalls: list[dict[str, Any]],
    *,
    capacity_budget: int | None,
    resolve_conflicts: bool,
) -> dict[str, dict[str, bool]]:
    """Separate code availability, run configuration, and observed execution.

    A feature can be implemented without a production caller and can be configured
    only by this benchmark. ``fired`` is derived from emitted lifecycle data, so
    clients do not have to infer it from a static wiring flag.
    """
    operation_kinds = {str(event.get("op", "")) for event in events}
    contradiction_quarantine_fired = any(
        event.get("op") == "QUARANTINE"
        and "quarantine:repeated_explicit_contradiction"
        in (event.get("after") or {}).get("tags", [])
        for event in events
    )
    return {
        "decay": {
            "implemented": _IMPLEMENTED_MECHANISMS["decay"],
            "configured": True,
            "fired": bool({"DECAY", "FORGET"}.intersection(operation_kinds)),
        },
        "eviction": {
            "implemented": _IMPLEMENTED_MECHANISMS["eviction"],
            "configured": capacity_budget is not None,
            "fired": "EVICT" in operation_kinds,
        },
        "contradiction_quarantine": {
            "implemented": _IMPLEMENTED_MECHANISMS["contradiction_quarantine"],
            "configured": CAPABILITIES["contradiction_quarantine_wired"],
            "fired": contradiction_quarantine_fired,
        },
        "conflict_update": {
            "implemented": _IMPLEMENTED_MECHANISMS["conflict_update"],
            "configured": resolve_conflicts,
            "fired": "SUPERSEDE" in operation_kinds,
        },
        "retrieval_scoring": {
            "implemented": _IMPLEMENTED_MECHANISMS["retrieval_scoring"],
            "configured": True,
            "fired": any(row.get("retrieval_candidates") for row in recalls),
        },
        "context_drop_provenance": {
            "implemented": _IMPLEMENTED_MECHANISMS["context_drop_provenance"],
            "configured": True,
            "fired": any(row.get("context_drops") for row in recalls),
        },
        "update_text_mutation": {
            "implemented": _IMPLEMENTED_MECHANISMS["update_text_mutation"],
            "configured": False,
            "fired": False,
        },
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
        "relations": [relation.model_dump(mode="json") for relation in record.relations],
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
        "relations": [relation.model_dump(mode="json") for relation in record.relations],
        "first_observed_at": (
            record.first_observed_at.isoformat() if record.first_observed_at else None
        ),
        "last_observed_at": (
            record.last_observed_at.isoformat() if record.last_observed_at else None
        ),
        "event_type": record.event_type,
        "config_version": record.config_version,
        "metric_window": dict(record.metric_window),
        "baseline_delta": dict(record.baseline_delta),
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
    ranked_ev = _first(events, "memory_candidates_ranked")

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
        "retrieval_candidates": list(ranked_ev.payload.get("candidates", [])) if ranked_ev else [],
        "included_memory_ids": included,
        "dropped_memory_ids": dropped,
        "context_drops": context_drops,
        "probes": probes,
        "shortcut": any(e.kind == "memory_shortcut" for e in events),
        "resolved": resolved_ev is not None,
        "resolved_memory_ids": resolved_ids,
    }
