"""Deterministic reconstruction of typed memory-event histories.

Associative ``similar_to`` links are intentionally excluded: they help recall,
but do not prove ordering or propagation.  This module only follows relation
types whose direction has an explicit temporal, causal, or topology meaning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from core.memory.store import MemoryRecord


_FORWARD = {"precedes", "causes", "propagates_to", "supersedes"}
_REVERSE = {"follows", "caused_by", "propagated_from", "superseded_by"}
_EARLY_CHANGE_TYPES = {"config_change", "topology_change", "baseline_deviation"}


@dataclass(frozen=True)
class EvolutionChain:
    memory_ids: tuple[str, ...]
    early_change_id: str | None


@dataclass(frozen=True)
class EvolutionFinding:
    chain: EvolutionChain
    relation_types: tuple[str, ...]
    asset_path: tuple[str, ...]
    hidden_failure_pattern: bool
    verified: bool


def reconstruct_evolution(
    memories: Iterable[MemoryRecord],
    *,
    current_assets: Iterable[str] = (),
    max_hops: int = 8,
) -> EvolutionChain | None:
    """Return the longest evidence-backed chain that reaches the current scope."""
    if max_hops < 1:
        raise ValueError("max_hops must be positive")
    records = {record.memory_id: record for record in memories if not record.quarantined}
    if not records:
        return None
    edges: dict[str, set[str]] = {memory_id: set() for memory_id in records}
    indegree: dict[str, int] = {memory_id: 0 for memory_id in records}
    for source in records.values():
        for relation in source.relations:
            if relation.target_id not in records:
                continue
            if relation.relation_type in _FORWARD:
                before, after = source.memory_id, relation.target_id
            elif relation.relation_type in _REVERSE:
                before, after = relation.target_id, source.memory_id
            else:
                continue
            if after not in edges[before]:
                edges[before].add(after)
                indegree[after] += 1

    scoped_assets = set(current_assets)
    targets = {
        record.memory_id
        for record in records.values()
        if not scoped_assets or scoped_assets.intersection(record.asset_ids)
    }
    if not targets:
        return None

    def observed(memory_id: str) -> datetime:
        record = records[memory_id]
        return record.first_observed_at or datetime.min.replace(tzinfo=timezone.utc)

    roots = sorted(
        (memory_id for memory_id, degree in indegree.items() if degree == 0),
        key=observed,
    ) or sorted(records, key=observed)
    best: tuple[str, ...] = ()

    def walk(path: tuple[str, ...]) -> None:
        nonlocal best
        tail = path[-1]
        if tail in targets and len(path) > len(best):
            best = path
        if len(path) >= max_hops + 1:
            return
        for child in sorted(edges[tail], key=observed):
            if child not in path:
                walk((*path, child))

    for root in roots:
        walk((root,))
    if len(best) < 2:
        return None
    early = next(
        (
            memory_id
            for memory_id in best
            if records[memory_id].event_type in _EARLY_CHANGE_TYPES
            or bool(records[memory_id].baseline_delta)
        ),
        None,
    )
    return EvolutionChain(best, early)


def evolution_context_line(chain: EvolutionChain, records: dict[str, MemoryRecord]) -> str:
    """Compact but attributable timeline line for the reasoning context."""
    stages = []
    for memory_id in chain.memory_ids:
        record = records[memory_id]
        at = record.first_observed_at.isoformat() if record.first_observed_at else "time-unknown"
        stages.append(f"{at}|{memory_id}|{record.event_type or 'event'}|{','.join(record.asset_ids)}")
    early = chain.early_change_id or "none"
    return f"evolution_chain early_change={early}: " + " => ".join(stages)


def analyze_evolution(
    memories: Iterable[MemoryRecord],
    *,
    current_assets: Iterable[str] = (),
    max_hops: int = 8,
) -> EvolutionFinding | None:
    """Explain a longitudinal chain and flag an early-change/late-failure pattern."""
    records = {record.memory_id: record for record in memories if not record.quarantined}
    chain = reconstruct_evolution(
        records.values(), current_assets=current_assets, max_hops=max_hops
    )
    if chain is None:
        return None
    relation_types: list[str] = []
    edges_verified: list[bool] = []
    for source_id, target_id in zip(chain.memory_ids, chain.memory_ids[1:]):
        source, target = records[source_id], records[target_id]
        forward = next(
            (
                relation
                for relation in source.relations
                if relation.target_id == target_id and relation.relation_type in _FORWARD
            ),
            None,
        )
        reverse = next(
            (
                relation
                for relation in target.relations
                if relation.target_id == source_id and relation.relation_type in _REVERSE
            ),
            None,
        )
        relation = forward or reverse
        if relation is None:
            return None
        relation_types.append(relation.relation_type)
        temporal = relation.relation_type in {"precedes", "follows"}
        edges_verified.append(
            bool(relation.evidence_ids)
            or (
                temporal
                and source.first_observed_at is not None
                and target.first_observed_at is not None
            )
        )
    asset_path = tuple(
        dict.fromkeys(
            asset
            for memory_id in chain.memory_ids
            for asset in records[memory_id].asset_ids
        )
    )
    final_event = records[chain.memory_ids[-1]].event_type or ""
    hidden_pattern = bool(
        chain.early_change_id
        and len(asset_path) >= 2
        and final_event in {"visible_failure", "outage", "alarm"}
    )
    return EvolutionFinding(
        chain=chain,
        relation_types=tuple(relation_types),
        asset_path=asset_path,
        hidden_failure_pattern=hidden_pattern,
        verified=all(edges_verified),
    )
