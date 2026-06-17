from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MemoryTier = Literal["episodic", "semantic", "procedural", "asset_profile"]


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


class TieredMemoryStore:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._records: list[MemoryRecord] = []

    def seed(self, records: list[MemoryRecord]) -> None:
        self._records.extend(records)

    def add(self, record: MemoryRecord) -> None:
        self._records.append(record)

    def quarantine(self, memory_id: str, reason: str) -> None:
        for record in self._records:
            if record.memory_id == memory_id:
                record.quarantined = True
                record.tags.append(f"quarantine:{reason}")

    def retrieve(
        self,
        query_terms: list[str],
        asset_ids: list[str],
        limit_per_tier: int = 3,
    ) -> dict[str, list[MemoryRecord]]:
        if not self.enabled:
            return {"episodic": [], "semantic": [], "procedural": [], "asset_profile": []}

        query = {term.lower() for term in query_terms}
        assets = set(asset_ids)
        ranked: dict[str, list[tuple[float, MemoryRecord]]] = {
            "episodic": [],
            "semantic": [],
            "procedural": [],
            "asset_profile": [],
        }
        for record in self._records:
            if record.quarantined:
                continue
            tag_hits = len(query.intersection({tag.lower() for tag in record.tags}))
            asset_hits = len(assets.intersection(record.asset_ids))
            text_hits = sum(1 for term in query if term and term in record.text.lower())
            evidence_hits = tag_hits + asset_hits + text_hits
            if evidence_hits > 0:
                score = (2.0 * asset_hits) + tag_hits + (0.25 * text_hits) + record.confidence
                ranked[record.tier].append((score, record))

        return {
            tier: [record for _, record in sorted(items, key=lambda item: item[0], reverse=True)[:limit_per_tier]]
            for tier, items in ranked.items()
        }
