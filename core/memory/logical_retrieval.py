"""Structured logical retrieval over the topology graph memory.

``logical_retrieve`` replaces fuzzy embedding similarity with explicit
entity / relation-path logic: a record is only a candidate if it is reachable
from a queried entity (or names one), and it is only ever returned if its
provenance cites observed evidence — un-observed records are architecturally
unretrievable, which is what keeps hallucinated "facts" out of the context.

``naive_similarity_retrieve`` is the honest baseline it is measured against:
bag-of-words overlap with no graph, no provenance gate.
"""
from __future__ import annotations

import re
from typing import Iterable

from core.memory.topo_graph import RelationType, TopoGraphMemory, TopoRecord

_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "with", "at", "by", "from", "into", "over", "under", "this", "that", "which", "what", "why",
    "how",
}

_VALID_RELATIONS: frozenset[str] = frozenset({"topology", "causal", "temporal"})
_DEFAULT_PATH_DEPTH = 4

# ── logical ranking weights ───────────────────────────────────────────────────
# Deterministic, documented preferences (largest first): graph proximity to a
# queried entity dominates, then a causally-linked root cause for an RCA intent,
# then intent/entity/relation compatibility, then provenance mass as tie-break.
_W_PROXIMITY_BASE = 24.0      # score at distance 0; decays linearly with hops
_W_PROXIMITY_PER_HOP = 5.0
_W_CAUSAL_ROOT_CAUSE = 24.0   # root_cause record with a causal edge, RCA intent
_W_INTENT_ROOT_CAUSE = 16.0   # entity_type matches an RCA intent
_W_INTENT_ALERT = 10.0
_W_INTENT_INCIDENT_STRONG = 8.0
_W_EXACT_ENTITY = 8.0         # query entity named directly (entity or attrs)
_W_INTENT_DEVICE = 6.0
_W_RELATION_MATCH = 4.0       # record carries an edge of the requested type
_W_INTENT_INCIDENT_WEAK = 4.0
_W_PER_EVIDENCE = 2.0         # provenance mass, capped
_EVIDENCE_CAP = 3
_UNREACHABLE_DISTANCE = 99    # sort-key distance for exact-only matches


def logical_retrieve(query: dict, graph: TopoGraphMemory, k: int) -> list[TopoRecord]:
    """Retrieve grounded records by entity and relation-path logic, not embeddings.

    Query shape:
      {"entities": [...], "relation": "topology" | "causal" | "temporal" | None,
       "intent": "...", "depth": int (optional, default 4)}

    Contract:
      * grounded-only — records without provenance evidence are never returned;
      * entity-anchored — with no query entities nothing is reachable, so the
        result is empty rather than a fuzzy-text guess;
      * deterministic — ties break on (distance, record id).
    """
    if k <= 0:
        return []
    entities = [str(e) for e in query.get("entities", []) if str(e).strip()]
    relation = query.get("relation")
    relation_type = relation if relation in _VALID_RELATIONS else None
    intent_tokens = set(_terms(str(query.get("intent") or "")))

    distances: dict[str, int] = {}
    max_depth = int(query.get("depth", _DEFAULT_PATH_DEPTH))
    for entity in entities:
        for ent, dist in graph.path_distances(entity, relation_type, max_depth).items():
            distances[ent] = min(distances.get(ent, dist), dist)

    scored: list[tuple[float, int, str, TopoRecord]] = []
    for rec in graph.all_records():
        if not graph.is_grounded(rec.id):
            continue
        dist = _record_distance(rec, distances)
        exact = any(_same_entity(e, rec.entity) or _entity_in_attrs(e, rec.attrs) for e in entities)
        if dist is None and not exact:
            continue

        score = 0.0
        if exact:
            score += _W_EXACT_ENTITY
        if dist is not None:
            score += max(0.0, _W_PROXIMITY_BASE - (_W_PROXIMITY_PER_HOP * dist))
        if relation_type and any(rel.type == relation_type for rel in rec.relations):
            score += _W_RELATION_MATCH
        score += _intent_score(intent_tokens, rec)
        if _is_root_cause_intent(intent_tokens) and rec.entity_type == "root_cause":
            if any(rel.type == "causal" for rel in rec.relations):
                score += _W_CAUSAL_ROOT_CAUSE
        score += min(_EVIDENCE_CAP, len(rec.provenance.evidence_ids)) * _W_PER_EVIDENCE

        if score > 0:
            scored.append((score, dist if dist is not None else _UNREACHABLE_DISTANCE, rec.id, rec))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [rec for _, _, _, rec in scored[:k]]


def naive_similarity_retrieve(query: dict, records: Iterable[TopoRecord | dict], k: int) -> list[TopoRecord]:
    """Naive RAG stand-in: bag-of-words overlap against flattened records.

    This intentionally has no graph traversal and no provenance filter, so it can
    retrieve textually similar but ungrounded or topologically unrelated records.
    Deterministic: ties break on record id.
    """
    if k <= 0:
        return []
    qtext = " ".join([
        " ".join(str(e) for e in query.get("entities", [])),
        str(query.get("relation") or ""),
        str(query.get("intent") or ""),
    ])
    qterms = set(_terms(qtext))
    scored: list[tuple[float, str, TopoRecord]] = []
    for raw in records:
        rec = raw if isinstance(raw, TopoRecord) else TopoRecord.model_validate(raw)
        rterms = _terms(_flatten_record(rec))
        if not rterms:
            continue
        overlap = qterms.intersection(rterms)
        if not overlap:
            continue
        score = len(overlap) / len(qterms)
        score += 0.01 * sum(1 for term in rterms if term in qterms)
        scored.append((score, rec.id, rec))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rec for _, _, rec in scored[:k]]


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP]


def _flatten_record(rec: TopoRecord) -> str:
    relation_text = " ".join(f"{rel.type} {rel.target}" for rel in rec.relations)
    return " ".join([
        rec.id,
        rec.entity,
        rec.entity_type,
        relation_text,
        _flatten_value(rec.attrs),
        rec.provenance.source,
        " ".join(rec.provenance.evidence_ids),
    ])


def _flatten_value(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten_value(v)}" for k, v in sorted(value.items()))
    if isinstance(value, list):
        return " ".join(_flatten_value(v) for v in value)
    return str(value)


def _same_entity(a: str, b: str) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def _entity_in_attrs(entity: str, attrs: dict) -> bool:
    needle = str(entity).strip().lower()
    return needle in {term.lower() for term in _flatten_value(attrs).split()}


def _record_distance(rec: TopoRecord, distances: dict[str, int]) -> int | None:
    """Hops from the nearest queried entity to this record (entity itself, or one
    hop past a relation target); None when unreachable."""
    candidates: list[int] = []
    rec_entity = rec.entity.strip().lower()
    if rec_entity in distances:
        candidates.append(distances[rec_entity])
    for rel in rec.relations:
        target = rel.target.strip().lower()
        if target in distances:
            candidates.append(distances[target] + 1)
    return min(candidates) if candidates else None


def _intent_score(intent_tokens: set[str], rec: TopoRecord) -> float:
    if not intent_tokens:
        return 0.0
    score = 0.0
    if _is_root_cause_intent(intent_tokens):
        if rec.entity_type == "root_cause":
            score += _W_INTENT_ROOT_CAUSE
        elif rec.entity_type == "incident":
            score += _W_INTENT_INCIDENT_WEAK
    if "alert" in intent_tokens and rec.entity_type == "alert":
        score += _W_INTENT_ALERT
    if "incident" in intent_tokens and rec.entity_type == "incident":
        score += _W_INTENT_INCIDENT_STRONG
    if rec.entity_type == "device" and "device" in intent_tokens:
        score += _W_INTENT_DEVICE
    return score


def _is_root_cause_intent(intent_tokens: set[str]) -> bool:
    return bool({"root", "cause"} & intent_tokens or "rca" in intent_tokens)
