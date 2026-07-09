from __future__ import annotations

from collections import deque
from typing import Literal

from pydantic import BaseModel, Field


EntityType = Literal["device", "link", "incident", "root_cause", "alert"]
RelationType = Literal["topology", "causal", "temporal"]


class TopoRelation(BaseModel):
    target: str
    type: RelationType


class TopoProvenance(BaseModel):
    evidence_ids: list[str] = Field(default_factory=list)
    source: str = ""
    ts: str = ""


class TopoRecord(BaseModel):
    id: str
    entity: str
    entity_type: EntityType
    relations: list[TopoRelation] = Field(default_factory=list)
    attrs: dict = Field(default_factory=dict)
    provenance: TopoProvenance = Field(default_factory=TopoProvenance)


class TopoGraphMemory:
    """Structured topology/entity memory with explicit evidence provenance.

    Records are stored as observed facts. Topology traversal is entity-level and
    treats relation edges as bidirectional because network topology adjacency is
    usually queried from either endpoint. Causal and temporal edges are also
    discoverable in both directions for incident investigation, while the relation
    type still gates which edges are traversed.
    """

    def __init__(self, records: list[TopoRecord | dict] | None = None):
        self._records: list[TopoRecord] = []
        self._by_id: dict[str, TopoRecord] = {}
        self._by_entity: dict[str, list[TopoRecord]] = {}
        for record in records or []:
            self.add_record(record)

    def add_record(self, record: TopoRecord | dict) -> TopoRecord:
        rec = record if isinstance(record, TopoRecord) else TopoRecord.model_validate(record)
        if rec.id in self._by_id:
            raise ValueError(f"duplicate topology memory record id: {rec.id}")
        self._records.append(rec)
        self._by_id[rec.id] = rec
        self._by_entity.setdefault(_norm(rec.entity), []).append(rec)
        return rec

    def all_records(self) -> list[TopoRecord]:
        return list(self._records)

    def get(self, record_id: str) -> TopoRecord | None:
        return self._by_id.get(record_id)

    def query_by_entity(self, entity: str) -> list[TopoRecord]:
        key = _norm(entity)
        direct = list(self._by_entity.get(key, []))
        attr_matches = [
            rec
            for rec in self._records
            if rec not in direct and _matches_entity_value(entity, rec.attrs)
        ]
        return direct + attr_matches

    def neighbors(self, entity: str) -> list[TopoRecord]:
        neighbour_entities = self._adjacent_entities(entity, None)
        seen: set[str] = set()
        out: list[TopoRecord] = []
        for ent in sorted(neighbour_entities):
            for rec in self.query_by_entity(ent):
                if rec.id not in seen:
                    seen.add(rec.id)
                    out.append(rec)
        return out

    def query_by_path(self, start: str, relation_type: RelationType, depth: int) -> list[TopoRecord]:
        distances = self.path_distances(start, relation_type, depth)
        seen: set[str] = set()
        out: list[TopoRecord] = []
        for entity, dist in sorted(distances.items(), key=lambda item: (item[1], item[0])):
            if dist == 0:
                continue
            for rec in self.query_by_entity(entity):
                if rec.id not in seen:
                    seen.add(rec.id)
                    out.append(rec)
        return out

    def path_distances(self, start: str, relation_type: RelationType | None, depth: int) -> dict[str, int]:
        """Shortest entity distances reachable through relation_type edges."""
        start_entities = {_norm(start)}
        for rec in self.query_by_entity(start):
            start_entities.add(_norm(rec.entity))

        distances = {entity: 0 for entity in start_entities}
        q = deque((entity, 0) for entity in sorted(start_entities))
        while q:
            entity, dist = q.popleft()
            if dist >= depth:
                continue
            for nxt in sorted(self._adjacent_entities(entity, relation_type)):
                if nxt not in distances:
                    distances[nxt] = dist + 1
                    q.append((nxt, dist + 1))
        return distances

    def is_grounded(self, record_id: str) -> bool:
        rec = self.get(record_id)
        return bool(rec and rec.provenance.evidence_ids)

    def _adjacent_entities(self, entity: str, relation_type: RelationType | None) -> set[str]:
        entity_key = _norm(entity)
        adjacent: set[str] = set()

        for rec in self._records:
            rec_key = _norm(rec.entity)
            for rel in rec.relations:
                if relation_type is not None and rel.type != relation_type:
                    continue
                target_key = _norm(rel.target)
                if rec_key == entity_key:
                    adjacent.add(target_key)
                if target_key == entity_key:
                    adjacent.add(rec_key)
        return adjacent


def _norm(value: str) -> str:
    return str(value).strip().lower()


def _matches_entity_value(entity: str, value) -> bool:
    needle = _norm(entity)
    if isinstance(value, dict):
        return any(_matches_entity_value(entity, v) for v in value.values())
    if isinstance(value, list):
        return any(_matches_entity_value(entity, v) for v in value)
    return needle == _norm(str(value))
