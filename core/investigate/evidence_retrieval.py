"""Deterministic evidence retrieval for a bounded investigation scope.

The caller supplies candidates collected by structured adapters and knowledge
indexes.  This module applies device, incident-time, source and product-version
constraints *before* any textual ranking, then compiles a small context pool:

* ``current_fact`` records are structured observations and always rank first;
* ``document`` records explain product behaviour;
* ``historical_incident`` records propose precedents but can never confirm the
  current incident;
* ``hypothesis`` records remain explicitly unverified.

Natural-language candidates use zero-dependency BM25, an optional injected
dense ranker, and bounded entity-relation expansion.  Their rankings are fused
with Reciprocal Rank Fusion (RRF).  The output accounts for every input as kept
or dropped and records the reason, which makes retrieval failures measurable
without exposing that bookkeeping in the operator's primary view.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, Sequence

from core.memory.bm25 import BM25Index, tokenize


EvidenceKind = Literal[
    "current_fact",
    "document",
    "historical_incident",
    "hypothesis",
]
EvidencePolarity = Literal["supports", "opposes", "neutral"]

_KINDS = {"current_fact", "document", "historical_incident", "hypothesis"}
_POLARITIES = {"supports", "opposes", "neutral"}
_REFERENCE_KINDS = {"document", "historical_incident", "hypothesis"}


def _normalise_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _normalise_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """One current observation, reference passage, precedent, or hypothesis.

    ``valid_from`` and ``valid_to`` describe applicability. ``observed_at`` is
    when a fact or historical incident happened.  ``claimed_decisive`` is an
    upstream claim only; :class:`RetrievedEvidence` computes whether the item is
    allowed to confirm the current incident.
    """

    evidence_id: str
    text: str
    kind: EvidenceKind
    source: str
    asset_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    applicable_versions: tuple[str, ...] = ()
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    polarity: EvidencePolarity = "neutral"
    hypothesis_ids: tuple[str, ...] = ()
    claimed_decisive: bool = False
    upstream_rank: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id must be non-empty")
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported evidence kind: {self.kind!r}")
        if self.polarity not in _POLARITIES:
            raise ValueError(f"unsupported evidence polarity: {self.polarity!r}")
        if not self.source.strip():
            raise ValueError("source must be non-empty")
        if self.upstream_rank is not None and self.upstream_rank <= 0:
            raise ValueError("upstream_rank must be positive")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        object.__setattr__(self, "asset_ids", _normalise_ids(self.asset_ids))
        object.__setattr__(self, "entity_ids", _normalise_ids(self.entity_ids))
        object.__setattr__(
            self, "applicable_versions", _normalise_ids(self.applicable_versions)
        )
        object.__setattr__(self, "hypothesis_ids", _normalise_ids(self.hypothesis_ids))
        object.__setattr__(self, "observed_at", _normalise_datetime(self.observed_at))
        object.__setattr__(self, "valid_from", _normalise_datetime(self.valid_from))
        object.__setattr__(self, "valid_to", _normalise_datetime(self.valid_to))
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")


@dataclass(frozen=True, slots=True)
class EntityRelation:
    """A time-bounded entity relation used for limited-hop expansion."""

    left: str
    right: str
    source: str = "entity-index"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    bidirectional: bool = True

    def __post_init__(self) -> None:
        if not self.left.strip() or not self.right.strip():
            raise ValueError("relation endpoints must be non-empty")
        object.__setattr__(self, "valid_from", _normalise_datetime(self.valid_from))
        object.__setattr__(self, "valid_to", _normalise_datetime(self.valid_to))
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Constraints derived from the current incident, before retrieval begins."""

    query_text: str
    asset_ids: tuple[str, ...]
    incident_start: datetime
    incident_end: datetime
    allowed_sources: tuple[str, ...] = ()
    device_versions: tuple[str, ...] = ()
    asset_versions: Mapping[str, str] = field(default_factory=dict)
    seed_entities: tuple[str, ...] = ()
    target_hypothesis_ids: tuple[str, ...] = ()
    history_since: datetime | None = None
    max_relation_hops: int = 2

    def __post_init__(self) -> None:
        start = _normalise_datetime(self.incident_start)
        end = _normalise_datetime(self.incident_end)
        if start is None or end is None:
            raise ValueError("incident_start and incident_end are required")
        if start > end:
            raise ValueError("incident_start must not be after incident_end")
        if self.max_relation_hops < 0 or self.max_relation_hops > 4:
            raise ValueError("max_relation_hops must be between 0 and 4")
        object.__setattr__(self, "incident_start", start)
        object.__setattr__(self, "incident_end", end)
        object.__setattr__(self, "history_since", _normalise_datetime(self.history_since))
        object.__setattr__(self, "asset_ids", _normalise_ids(self.asset_ids))
        object.__setattr__(self, "allowed_sources", _normalise_ids(self.allowed_sources))
        object.__setattr__(self, "device_versions", _normalise_ids(self.device_versions))
        normalised_asset_versions = {
            str(asset_id).strip(): str(version).strip()
            for asset_id, version in self.asset_versions.items()
            if str(asset_id).strip() and str(version).strip()
        }
        object.__setattr__(
            self,
            "asset_versions",
            MappingProxyType(dict(sorted(normalised_asset_versions.items()))),
        )
        object.__setattr__(self, "seed_entities", _normalise_ids(self.seed_entities))
        object.__setattr__(
            self, "target_hypothesis_ids", _normalise_ids(self.target_hypothesis_ids)
        )


class DenseRanker(Protocol):
    """Injected semantic retrieval adapter; no model dependency lives here."""

    def rank(
        self,
        query_text: str,
        candidates: Sequence[EvidenceCandidate],
        k: int,
    ) -> Sequence[str | tuple[str, float]]: ...


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    candidate: EvidenceCandidate
    rank: int
    score: float
    reasons: tuple[str, ...]
    decisive_for_current_incident: bool

    @property
    def context_section(self) -> EvidenceKind:
        return self.candidate.kind


@dataclass(frozen=True, slots=True)
class DroppedEvidence:
    candidate: EvidenceCandidate
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRetrievalResult:
    kept: tuple[RetrievedEvidence, ...]
    dropped: tuple[DroppedEvidence, ...]
    expanded_entities: tuple[str, ...]
    dense_used: bool

    def context_sections(self) -> dict[EvidenceKind, tuple[RetrievedEvidence, ...]]:
        """Return explicit context sections so references cannot look like facts."""

        return {
            kind: tuple(item for item in self.kept if item.candidate.kind == kind)
            for kind in (
                "current_fact",
                "document",
                "historical_incident",
                "hypothesis",
            )
        }


class EvidenceRetriever:
    """Prefilter, retrieve, fuse, and compile evidence for one incident turn."""

    def __init__(
        self,
        candidates: Sequence[EvidenceCandidate],
        *,
        relations: Sequence[EntityRelation] = (),
        dense_ranker: DenseRanker | None = None,
        rrf_c: int = 60,
    ) -> None:
        if rrf_c < 0:
            raise ValueError("rrf_c must be non-negative")
        by_id: dict[str, EvidenceCandidate] = {}
        for candidate in candidates:
            if candidate.evidence_id in by_id:
                raise ValueError(f"duplicate evidence_id: {candidate.evidence_id!r}")
            by_id[candidate.evidence_id] = candidate
        self._candidates = tuple(by_id[key] for key in sorted(by_id))
        self._relations = tuple(
            sorted(relations, key=lambda edge: (edge.left, edge.right, edge.source))
        )
        self._dense_ranker = dense_ranker
        self._rrf_c = rrf_c

    def retrieve(
        self,
        scope: RetrievalScope,
        *,
        top_k: int = 12,
        fusion_depth: int = 60,
    ) -> EvidenceRetrievalResult:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if fusion_depth <= 0:
            raise ValueError("fusion_depth must be positive")

        distances = self._expand_entities(scope)
        eligible: list[EvidenceCandidate] = []
        dropped: list[DroppedEvidence] = []
        for candidate in self._candidates:
            reasons = self._prefilter_reasons(candidate, scope, distances)
            if reasons:
                dropped.append(DroppedEvidence(candidate, tuple(reasons)))
            else:
                eligible.append(candidate)

        current_facts = [item for item in eligible if item.kind == "current_fact"]
        references = [item for item in eligible if item.kind in _REFERENCE_KINDS]
        current_facts.sort(key=lambda item: self._fact_sort_key(item, distances))

        reference_by_id = {item.evidence_id: item for item in references}
        rankings: list[list[str]] = []
        route_reasons: dict[str, list[str]] = {item.evidence_id: [] for item in references}

        upstream_ranking = [
            item.evidence_id
            for item in sorted(
                (candidate for candidate in references if candidate.upstream_rank is not None),
                key=lambda candidate: (candidate.upstream_rank or 0, candidate.evidence_id),
            )
        ]
        if upstream_ranking:
            rankings.append(upstream_ranking)
            for position, evidence_id in enumerate(upstream_ranking, start=1):
                route_reasons[evidence_id].append(f"upstream_rank:{position}")

        if references and scope.query_text.strip():
            bm25 = BM25Index(
                {
                    item.evidence_id: tokenize(self._indexable_text(item))
                    for item in references
                }
            )
            bm25_ranking = bm25.rank(scope.query_text, min(fusion_depth, len(references)))
            if bm25_ranking:
                rankings.append(bm25_ranking)
                for position, evidence_id in enumerate(bm25_ranking, start=1):
                    route_reasons[evidence_id].append(f"bm25_rank:{position}")

        if references and self._dense_ranker is not None:
            raw_dense = self._dense_ranker.rank(
                scope.query_text,
                tuple(references),
                min(fusion_depth, len(references)),
            )
            dense_ranking = self._normalise_dense_ranking(raw_dense, reference_by_id)
            if dense_ranking:
                rankings.append(dense_ranking)
                for position, evidence_id in enumerate(dense_ranking, start=1):
                    route_reasons[evidence_id].append(f"dense_rank:{position}")

        relation_ranking = self._relation_ranking(references, distances)
        if relation_ranking:
            rankings.append(relation_ranking)
            for position, evidence_id in enumerate(relation_ranking, start=1):
                route_reasons[evidence_id].append(f"relation_rank:{position}")

        fused_scores = self._rrf_scores(rankings)
        ranked_references = sorted(
            (reference_by_id[evidence_id] for evidence_id in fused_scores),
            key=lambda item: (-fused_scores[item.evidence_id], item.evidence_id),
        )

        ranked = current_facts + ranked_references
        selected = self._select_with_polarity(
            ranked,
            top_k=top_k,
            current_fact_count=len(current_facts),
            scope=scope,
        )
        selected_ids = {item.evidence_id for item in selected}

        kept: list[RetrievedEvidence] = []
        for rank, candidate in enumerate(selected, start=1):
            reasons: list[str]
            if candidate.kind == "current_fact":
                reasons = ["structured_fact_priority"]
                score = 1.0
            else:
                reasons = list(route_reasons[candidate.evidence_id])
                score = fused_scores[candidate.evidence_id]
            if candidate.polarity in {"supports", "opposes"}:
                reasons.append(f"{candidate.polarity}_hypothesis")
            if candidate.kind == "historical_incident" and candidate.claimed_decisive:
                reasons.append("historical_incident_is_reference_only")
            kept.append(
                RetrievedEvidence(
                    candidate=candidate,
                    rank=rank,
                    score=round(score, 8),
                    reasons=tuple(reasons),
                    decisive_for_current_incident=(
                        candidate.kind == "current_fact" and candidate.claimed_decisive
                    ),
                )
            )

        already_dropped = {item.candidate.evidence_id for item in dropped}
        for candidate in eligible:
            if candidate.evidence_id in selected_ids or candidate.evidence_id in already_dropped:
                continue
            reason = (
                "top_k_limit"
                if candidate.kind == "current_fact" or candidate.evidence_id in fused_scores
                else "no_retrieval_signal"
            )
            dropped.append(DroppedEvidence(candidate, (reason,)))

        dropped.sort(key=lambda item: item.candidate.evidence_id)
        return EvidenceRetrievalResult(
            kept=tuple(kept),
            dropped=tuple(dropped),
            expanded_entities=tuple(sorted(distances)),
            dense_used=self._dense_ranker is not None,
        )

    @staticmethod
    def _indexable_text(candidate: EvidenceCandidate) -> str:
        return " ".join((candidate.text, *candidate.entity_ids, *candidate.asset_ids))

    @staticmethod
    def _normalise_dense_ranking(
        ranking: Sequence[str | tuple[str, float]],
        allowed: Mapping[str, EvidenceCandidate],
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in ranking:
            evidence_id = str(item[0] if isinstance(item, tuple) else item)
            if evidence_id in allowed and evidence_id not in seen:
                result.append(evidence_id)
                seen.add(evidence_id)
        return result

    def _expand_entities(self, scope: RetrievalScope) -> dict[str, int]:
        seeds = set(scope.seed_entities)
        seeds.update(f"asset:{asset_id}" for asset_id in scope.asset_ids)
        distances = {entity: 0 for entity in seeds}
        if not seeds or scope.max_relation_hops == 0:
            return distances

        adjacency: dict[str, set[str]] = {}
        for relation in self._relations:
            if not self._relation_valid(relation, scope.incident_end):
                continue
            adjacency.setdefault(relation.left, set()).add(relation.right)
            if relation.bidirectional:
                adjacency.setdefault(relation.right, set()).add(relation.left)

        queue: deque[str] = deque(sorted(seeds))
        while queue:
            current = queue.popleft()
            distance = distances[current]
            if distance >= scope.max_relation_hops:
                continue
            for neighbour in sorted(adjacency.get(current, ())):
                if neighbour in distances:
                    continue
                distances[neighbour] = distance + 1
                queue.append(neighbour)
        return distances

    @staticmethod
    def _relation_valid(relation: EntityRelation, as_of: datetime) -> bool:
        if relation.valid_from and as_of < relation.valid_from:
            return False
        if relation.valid_to and as_of > relation.valid_to:
            return False
        return True

    @staticmethod
    def _prefilter_reasons(
        candidate: EvidenceCandidate,
        scope: RetrievalScope,
        distances: Mapping[str, int],
    ) -> list[str]:
        reasons: list[str] = []
        scope_assets = set(scope.asset_ids)
        candidate_assets = set(candidate.asset_ids)
        if scope_assets and candidate_assets and not scope_assets.intersection(candidate_assets):
            reasons.append("asset_mismatch")

        allowed_sources = {source.casefold() for source in scope.allowed_sources}
        if allowed_sources and candidate.source.casefold() not in allowed_sources:
            reasons.append("source_not_allowed")

        matching_assets = scope_assets.intersection(candidate_assets)
        scoped_versions = {
            scope.asset_versions[asset_id].casefold()
            for asset_id in matching_assets
            if asset_id in scope.asset_versions
        }
        if candidate_assets:
            current_versions = scoped_versions or {
                version.casefold() for version in scope.device_versions
            }
        else:
            current_versions = {
                version.casefold() for version in scope.asset_versions.values()
            }
            current_versions.update(version.casefold() for version in scope.device_versions)
        applicable_versions = {
            version.casefold() for version in candidate.applicable_versions
        }
        if applicable_versions and not current_versions:
            reasons.append("device_version_unknown")
        elif applicable_versions and not applicable_versions.intersection(current_versions):
            reasons.append("version_mismatch")

        as_of = scope.incident_end
        if candidate.valid_from and as_of < candidate.valid_from:
            reasons.append("not_yet_valid")
        if candidate.valid_to and as_of > candidate.valid_to:
            reasons.append("expired_at_incident")

        if candidate.kind == "current_fact":
            if candidate.observed_at is None:
                reasons.append("missing_observation_time")
            elif not scope.incident_start <= candidate.observed_at <= scope.incident_end:
                reasons.append("outside_incident_window")
        elif candidate.kind == "historical_incident" and candidate.observed_at:
            if candidate.observed_at > scope.incident_end:
                reasons.append("future_historical_incident")
            if scope.history_since and candidate.observed_at < scope.history_since:
                reasons.append("outside_history_window")

        # Asset identity is authoritative.  Entity expansion can attach an
        # unscoped record to the incident, but can never override a conflicting
        # asset id.  This is what prevents a reassigned IP from reviving the old
        # device's evidence.
        if (
            scope_assets
            and not candidate_assets
            and candidate.kind in {"current_fact", "historical_incident", "hypothesis"}
        ):
            if not set(candidate.entity_ids).intersection(distances):
                reasons.append("entity_unreachable")
        return reasons

    @staticmethod
    def _fact_sort_key(
        candidate: EvidenceCandidate,
        distances: Mapping[str, int],
    ) -> tuple[object, ...]:
        entity_distance = min(
            (distances[entity] for entity in candidate.entity_ids if entity in distances),
            default=10_000,
        )
        observed = candidate.observed_at or datetime.min.replace(tzinfo=timezone.utc)
        return (
            -int(candidate.claimed_decisive),
            entity_distance,
            -observed.timestamp(),
            candidate.evidence_id,
        )

    @staticmethod
    def _relation_ranking(
        candidates: Sequence[EvidenceCandidate],
        distances: Mapping[str, int],
    ) -> list[str]:
        scored: list[tuple[int, str]] = []
        for candidate in candidates:
            candidate_distances = [
                distances[entity]
                for entity in candidate.entity_ids
                if entity in distances
            ]
            if candidate_distances:
                scored.append((min(candidate_distances), candidate.evidence_id))
        scored.sort()
        return [evidence_id for _, evidence_id in scored]

    def _rrf_scores(self, rankings: Sequence[Sequence[str]]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for ranking in rankings:
            for position, evidence_id in enumerate(ranking, start=1):
                scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (
                    self._rrf_c + position
                )
        return scores

    @staticmethod
    def _select_with_polarity(
        ranked: Sequence[EvidenceCandidate],
        *,
        top_k: int,
        current_fact_count: int,
        scope: RetrievalScope,
    ) -> list[EvidenceCandidate]:
        selected = list(ranked[:top_k])
        if current_fact_count >= top_k:
            return selected

        protected_ids: set[str] = set()
        target_ids = set(scope.target_hypothesis_ids)

        def applies(item: EvidenceCandidate, polarity: str) -> bool:
            if item.polarity != polarity:
                return False
            return not target_ids or not item.hypothesis_ids or bool(
                target_ids.intersection(item.hypothesis_ids)
            )

        for polarity in ("supports", "opposes"):
            existing = next((item for item in selected if applies(item, polarity)), None)
            if existing is not None:
                protected_ids.add(existing.evidence_id)
                continue
            replacement = next((item for item in ranked if applies(item, polarity)), None)
            if replacement is None:
                continue
            replace_at = next(
                (
                    index
                    for index in range(len(selected) - 1, current_fact_count - 1, -1)
                    if selected[index].evidence_id not in protected_ids
                ),
                None,
            )
            if replace_at is not None:
                selected[replace_at] = replacement
                protected_ids.add(replacement.evidence_id)
            elif len(selected) < top_k:
                selected.append(replacement)
                protected_ids.add(replacement.evidence_id)

        # Replacements can point at an item already selected by the other route.
        deduplicated: list[EvidenceCandidate] = []
        seen: set[str] = set()
        for candidate in selected:
            if candidate.evidence_id not in seen:
                deduplicated.append(candidate)
                seen.add(candidate.evidence_id)
        if len(deduplicated) < min(top_k, len(ranked)):
            for candidate in ranked:
                if candidate.evidence_id in seen:
                    continue
                deduplicated.append(candidate)
                seen.add(candidate.evidence_id)
                if len(deduplicated) == top_k:
                    break
        return deduplicated


__all__ = [
    "DenseRanker",
    "DroppedEvidence",
    "EntityRelation",
    "EvidenceCandidate",
    "EvidenceKind",
    "EvidencePolarity",
    "EvidenceRetriever",
    "EvidenceRetrievalResult",
    "RetrievalScope",
    "RetrievedEvidence",
]
