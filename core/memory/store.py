"""Tiered long/short-term memory store.

Three learned tiers (episodic / semantic / procedural) plus seeded asset
profiles overlay into one queryable store. Retrieval is rule-based and
deterministic — no embeddings, no LLM — so every recall is reproducible and
attributable to explicit tag / asset / text evidence.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MemoryTier = Literal["episodic", "semantic", "procedural", "asset_profile"]

# Retrieval scoring weights (documented, not magic): shared assets are the
# strongest identity signal, exact tag matches next, free-text substring hits
# are weak corroboration. A record's own confidence (grown by verified reuse,
# see core/evolve) breaks ties toward memories that have survived verification.
_W_ASSET_HIT = 2.0
_W_TAG_HIT = 1.0
_W_TEXT_HIT = 0.25

_EMPTY_TIERS: tuple[MemoryTier, ...] = ("episodic", "semantic", "procedural", "asset_profile")


class MemoryRecord(BaseModel):
    memory_id: str
    tier: MemoryTier
    text: str
    tags: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    quarantined: bool = False
    source_trace_ids: list[str] = Field(default_factory=list)
    # provenance-linked snapshot of the evidence observed when this memory was written,
    # so a recurring incident can be resolved by recall instead of re-investigation.
    evidence_snapshot: list[dict] = Field(default_factory=list)
    # Phase B memory dynamics (see core/evolve/memory_ops.py):
    #   links      — A-MEM associative links to same-family memory_ids (Xu+ 2025)
    #   importance — Generative-Agents salience, gates reflection (Park+ 2023)
    #   strength   — Ebbinghaus retrievability; decays over time, reset on reuse (1885)
    links: list[str] = Field(default_factory=list)
    importance: float = 1.0
    strength: float = 1.0


class TieredMemoryStore:
    """In-process store of :class:`MemoryRecord` with tier-partitioned retrieval.

    ``memory_id`` is the primary key: adding a duplicate id raises so a record
    can never be silently shadowed. Quarantined records stay for audit but are
    excluded from ``active()`` and ``retrieve()``.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._records: list[MemoryRecord] = []
        self._by_id: dict[str, MemoryRecord] = {}

    def seed(self, records: list[MemoryRecord]) -> None:
        """Bulk-add prior records (e.g. asset profiles). Same id guard as ``add``."""
        for record in records:
            self.add(record)

    def add(self, record: MemoryRecord) -> None:
        """Append one record. Raises ``ValueError`` on a duplicate memory_id."""
        if record.memory_id in self._by_id:
            raise ValueError(f"duplicate memory_id: {record.memory_id}")
        self._records.append(record)
        self._by_id[record.memory_id] = record

    def get(self, memory_id: str) -> MemoryRecord | None:
        """Return the record with this id (quarantined included), or None."""
        return self._by_id.get(memory_id)

    def records(self) -> list[MemoryRecord]:
        """All records, quarantined included, in insertion order (copy)."""
        return list(self._records)

    def active(self) -> list[MemoryRecord]:
        """Non-quarantined records in insertion order (copy)."""
        return [r for r in self._records if not r.quarantined]

    def quarantine(self, memory_id: str, reason: str) -> None:
        """Mark a record untrusted; it stays for audit with a ``quarantine:<reason>`` tag."""
        record = self._by_id.get(memory_id)
        if record is not None:
            record.quarantined = True
            record.tags.append(f"quarantine:{reason}")

    def retrieve(
        self,
        query_terms: list[str],
        asset_ids: list[str],
        limit_per_tier: int = 3,
    ) -> dict[str, list[MemoryRecord]]:
        """Top ``limit_per_tier`` active records per tier for these terms/assets.

        A record must overlap the query on at least one tag, asset, or text term
        to be returned at all — no match, no recall. Ranking is deterministic:
        score descending, insertion order breaking ties.
        """
        if not self.enabled or limit_per_tier <= 0:
            return {tier: [] for tier in _EMPTY_TIERS}

        query = {term.lower() for term in query_terms}
        assets = set(asset_ids)
        # (score, insertion index, record): stable, explicit tie-breaking.
        ranked: dict[str, list[tuple[float, int, MemoryRecord]]] = {tier: [] for tier in _EMPTY_TIERS}
        for index, record in enumerate(self._records):
            if record.quarantined:
                continue
            tag_hits = len(query.intersection({tag.lower() for tag in record.tags}))
            asset_hits = len(assets.intersection(record.asset_ids))
            text_hits = sum(1 for term in query if term and term in record.text.lower())
            if tag_hits + asset_hits + text_hits > 0:
                score = (
                    _W_ASSET_HIT * asset_hits
                    + _W_TAG_HIT * tag_hits
                    + _W_TEXT_HIT * text_hits
                    + record.confidence
                )
                ranked[record.tier].append((score, index, record))

        return {
            tier: [
                record
                for _, _, record in sorted(items, key=lambda item: (-item[0], item[1]))[:limit_per_tier]
            ]
            for tier, items in ranked.items()
        }
