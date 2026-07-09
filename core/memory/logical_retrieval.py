from __future__ import annotations

import re
from typing import Iterable

from core.memory.topo_graph import RelationType, TopoGraphMemory, TopoRecord


_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "with", "at", "by", "from", "into", "over", "under", "this", "that", "which", "what", "why",
    "how",
}


def logical_retrieve(query: dict, graph: TopoGraphMemory, k: int) -> list[TopoRecord]:
    """Retrieve grounded records by entity and relation-path logic, not embeddings.

    Query shape:
      {"entities": [...], "relation": "topology" | "causal" | "temporal" | None,
       "intent": "..."}

    Ranking is deterministic and favors shortest path proximity, relation-type
    matches, intent-compatible record types, and stronger provenance. Ungrounded
    records are never returned.
    """
    entities = [str(e) for e in query.get("entities", []) if str(e).strip()]
    relation = query.get("relation")
    relation_type = relation if relation in {"topology", "causal", "temporal"} else None
    intent = str(query.get("intent", ""))
    intent_tokens = set(_terms(intent))

    distances: dict[str, int] = {}
    max_depth = int(query.get("depth", 4))
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
            score += 8.0
        if dist is not None:
            score += max(0.0, 24.0 - (5.0 * dist))
        if relation_type and any(rel.type == relation_type for rel in rec.relations):
            score += 4.0
        score += _intent_score(intent_tokens, rec)
        if _is_root_cause_intent(intent_tokens) and rec.entity_type == "root_cause":
            if any(rel.type == "causal" for rel in rec.relations):
                score += 24.0
        score += min(3, len(rec.provenance.evidence_ids)) * 2.0

        if score > 0:
            scored.append((score, dist if dist is not None else 99, rec.id, rec))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [rec for _, _, _, rec in scored[:k]]


def naive_similarity_retrieve(query: dict, records: Iterable[TopoRecord | dict], k: int) -> list[TopoRecord]:
    """Naive RAG stand-in: bag-of-words overlap against flattened records.

    This intentionally has no graph traversal and no provenance filter, so it can
    retrieve textually similar but ungrounded or topologically unrelated records.
    """
    qtext = " ".join([
        " ".join(str(e) for e in query.get("entities", [])),
        str(query.get("relation") or ""),
        str(query.get("intent", "")),
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
        score = len(overlap) / len(qterms or {""})
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


def _flatten_value(value) -> str:
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
            score += 16.0
        elif rec.entity_type == "incident":
            score += 4.0
    if "alert" in intent_tokens and rec.entity_type == "alert":
        score += 10.0
    if "incident" in intent_tokens and rec.entity_type == "incident":
        score += 8.0
    if rec.entity_type == "device" and "device" in intent_tokens:
        score += 6.0
    return score


def _is_root_cause_intent(intent_tokens: set[str]) -> bool:
    return bool({"root", "cause"} & intent_tokens or "rca" in intent_tokens)
