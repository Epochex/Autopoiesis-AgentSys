"""Tiered long/short-term memory store.

Three learned tiers (episodic / semantic / procedural) plus seeded asset
profiles overlay into one queryable store. Retrieval is rule-based and
deterministic — no embeddings, no LLM — so every recall is reproducible and
attributable to explicit lexical / asset / structural evidence.

Retrieval ranking (see :meth:`TieredMemoryStore.retrieve`) is a two-stage
lexical-then-structural design:

  1. **Lexical base** — Okapi BM25 (IDF-weighted term frequency, see
     :mod:`core.memory.bm25`) over each record's full document (its text tokens
     plus its tag labels), plus an exact-identity asset boost. BM25 replaces the
     earlier raw tag-overlap count, which under-ranked the truly relevant record
     whenever a common term matched many memories: on the LongMemEval-500 anchor,
     scoring the whole session text this way lifts the store's own recall@5 from
     0.906 to 0.970 — matching the BM25 lexical ceiling (0.970).
  2. **Structural rerank** — a bounded, deterministic prior built ONLY from the
     record's own lifecycle signals (tier, A-MEM link centrality, reflection
     importance, Ebbinghaus strength/recency, verified-reuse confidence). It is
     scaled to a small fraction of the top lexical score, so it reorders
     near-ties and expresses priors without overriding a clear lexical winner
     (RRF-style equal-vote fusion of a weak structural signal was measured to
     wreck precision; a bounded rerank does not). Toggle with ``use_structure``.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, Field

from core.memory.bm25 import tokenize
from core.memory.segmented_bm25 import SegmentedBM25Index


MemoryTier = Literal["episodic", "semantic", "procedural", "asset_profile"]

# --- lexical base weights ---------------------------------------------------
# Shared assets are the strongest identity signal (exact match, not lexical);
# BM25 over the record's full text + tags carries the lexical relevance.
_W_ASSET_HIT = 2.0

# --- structural rerank weights (Phase B memory dynamics) --------------------
# The structural prior is a convex blend (weights sum to 1) of five normalised
# lifecycle signals, then scaled by ``_STRUCT_COEF`` * (top lexical score) so it
# stays a bounded rerank. Chosen a priori (not fit to the eval): recency and
# link centrality carry the most retrieval-relevant structure, confidence and
# importance are verified-reuse priors, the tier prior is a mild identity nudge.
_STRUCT_COEF = 0.15
_S_STRENGTH = 0.30     # Ebbinghaus retrievability (recency); decays with age
_S_CENTRAL = 0.25      # A-MEM associative-link degree (family centrality)
_S_IMPORT = 0.20       # Generative-Agents reflection salience
_S_CONF = 0.15         # confidence grown by verified reuse
_S_TIER = 0.10         # tier identity prior
_LINK_CAP = 5          # link degree saturating this many neighbours -> 1.0
_CONF_CAP = 3.0        # confidence is capped here by apply_route
_TIER_PRIOR: dict[str, float] = {
    "asset_profile": 1.0,   # seeded identity facts — the most trustworthy prior
    "semantic": 0.75,       # reflected insights
    "procedural": 0.5,      # runbooks
    "episodic": 0.25,       # raw incident traces
}

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
    # Provenance-linked historical evidence.  It may explain why a memory exists,
    # but it is never current-run evidence; a recurrence must be freshly probed.
    evidence_snapshot: list[dict] = Field(default_factory=list)
    # Phase B memory dynamics (see core/evolve/memory_ops.py):
    #   links        — A-MEM associative links to same-family memory_ids (Xu+ 2025)
    #   importance   — Generative-Agents salience, gates reflection (Park+ 2023)
    #   strength     — Ebbinghaus retrievability; decays over time, reset on reuse (1885)
    #   access_count — how many times this record was recalled/reused (utility eviction)
    #   superseded_by— set when a newer memory contradicts this one (conflict-resolving UPDATE)
    links: list[str] = Field(default_factory=list)
    importance: float = 1.0
    strength: float = 1.0
    access_count: int = 0
    superseded_by: str | None = None


class MemoryRepository(Protocol):
    """Durable source used only at offline consolidation boundaries."""

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]: ...

    def sync_records(self, records: Sequence[MemoryRecord]) -> list[Any]: ...


def _structural_prior(record: "MemoryRecord") -> float:
    """Normalised structural salience in [0, 1] from the record's own lifecycle
    signals only — never from the query or any label. Constant across records
    when the signals are at their defaults, so an un-evolved store ranks by pure
    lexical score; it only reorders once the dynamics (decay/links/reflection)
    have written differentiating structure.
    """
    strength = min(1.0, max(0.0, record.strength))
    centrality = min(1.0, len(record.links) / _LINK_CAP)
    importance = 1.0 - 1.0 / (1.0 + max(0.0, record.importance))   # saturating salience
    confidence = min(1.0, max(0.0, (record.confidence - 1.0) / (_CONF_CAP - 1.0)))
    tier_prior = _TIER_PRIOR.get(record.tier, 0.25)
    return (
        _S_STRENGTH * strength
        + _S_CENTRAL * centrality
        + _S_IMPORT * importance
        + _S_CONF * confidence
        + _S_TIER * tier_prior
    )


class TieredMemoryStore:
    """In-process store of :class:`MemoryRecord` with tier-partitioned retrieval.

    ``memory_id`` is the primary key: adding a duplicate id raises so a record
    can never be silently shadowed. Quarantined records stay for audit but are
    excluded from ``active()`` and ``retrieve()``.
    """

    def __init__(
        self,
        enabled: bool = True,
        *,
        lexical_seal_threshold: int = 1_000,
        lexical_compact_segment_threshold: int = 8,
        repository: MemoryRepository | None = None,
    ):
        self.enabled = enabled
        self._records: list[MemoryRecord] = []
        self._by_id: dict[str, MemoryRecord] = {}
        self._indexed_assets: dict[str, set[str]] = {}
        self._asset_to_ids: dict[str, set[str]] = {}
        self._repository = repository
        self._lexical = SegmentedBM25Index(
            seal_threshold=lexical_seal_threshold,
            compact_segment_threshold=lexical_compact_segment_threshold,
        )

    @classmethod
    def from_repository(
        cls,
        repository: MemoryRepository,
        *,
        enabled: bool = True,
        lexical_seal_threshold: int = 1_000,
        lexical_compact_segment_threshold: int = 8,
    ) -> "TieredMemoryStore":
        """Restore complete memory records and rebuild the derived local index."""
        store = cls(
            enabled,
            lexical_seal_threshold=lexical_seal_threshold,
            lexical_compact_segment_threshold=lexical_compact_segment_threshold,
            repository=repository,
        )
        store.seed(repository.load_records(include_quarantined=True))
        return store

    def flush(self) -> list[Any]:
        """Atomically persist the current memory snapshot when a repository exists."""
        if self._repository is None:
            return []
        return self._repository.sync_records(self.records())

    @staticmethod
    def _lexical_tokens(record: MemoryRecord) -> list[str]:
        return tokenize(record.text) + [tag.lower() for tag in record.tags]

    def _unindex_assets(self, memory_id: str) -> bool:
        was_indexed = memory_id in self._indexed_assets
        for asset_id in self._indexed_assets.pop(memory_id, set()):
            ids = self._asset_to_ids.get(asset_id)
            if ids is None:
                continue
            ids.discard(memory_id)
            if not ids:
                del self._asset_to_ids[asset_id]
        return was_indexed

    def reindex(self, memory_id: str) -> bool:
        """Refresh one record after an in-place text, tag, or asset mutation.

        Memory records remain mutable because the evolution operators reinforce
        them in place.  Those operators call this method whenever a field used by
        retrieval changes; lifecycle-only fields such as confidence or strength do
        not require an index write.
        """
        record = self._by_id.get(memory_id)
        if record is None:
            return False
        was_indexed = self._unindex_assets(memory_id)
        if record.quarantined:
            if was_indexed:
                self._lexical.delete(memory_id)
            return True
        assets = set(record.asset_ids)
        self._indexed_assets[memory_id] = assets
        for asset_id in assets:
            self._asset_to_ids.setdefault(asset_id, set()).add(memory_id)
        self._lexical.upsert(memory_id, self._lexical_tokens(record))
        return True

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
        if not record.quarantined:
            self.reindex(record.memory_id)

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
            was_active = not record.quarantined
            record.quarantined = True
            record.tags.append(f"quarantine:{reason}")
            if was_active:
                self._unindex_assets(memory_id)
                self._lexical.delete(memory_id)

    def index_health(self) -> dict[str, int | float | bool]:
        """Expose measured lexical-index growth and compaction state."""
        return self._lexical.health()

    def compact_index(self, *, force: bool = False) -> bool:
        """Compact obsolete lexical versions without changing memory history."""
        return self._lexical.compact(force=force)

    def retrieve(
        self,
        query_terms: list[str],
        asset_ids: list[str],
        limit_per_tier: int = 3,
        *,
        use_structure: bool = True,
    ) -> dict[str, list[MemoryRecord]]:
        """Top ``limit_per_tier`` active records per tier for these terms/assets.

        A record must overlap the query on at least one BM25 term (text or tag) or
        asset to be a candidate at all — no match, no recall. Candidates are
        ordered by ``lexical_base + structural_prior`` (BM25 + asset identity,
        then a bounded structural rerank); ties break on insertion order.

        ``use_structure=False`` returns the pure lexical ranking (the honest
        BM25-only floor), used to isolate the structural rerank's contribution.
        """
        if not self.enabled or limit_per_tier <= 0:
            return {tier: [] for tier in _EMPTY_TIERS}

        query = [term.lower() for term in query_terms if term]
        assets = set(asset_ids)
        insertion_order = {record.memory_id: index for index, record in enumerate(self._records)}
        lexical_ids = {doc_id for doc_id, _ in self._lexical.rank_with_scores(query)}
        asset_candidate_ids = {
            memory_id
            for asset_id in assets
            for memory_id in self._asset_to_ids.get(asset_id, ())
        }
        candidate_ids = lexical_ids | asset_candidate_ids

        scored: list[tuple[float, int, float, MemoryRecord]] = []  # (base, index, struct_prior, rec)
        max_base = 0.0
        for memory_id in candidate_ids:
            record = self._by_id[memory_id]
            if record.quarantined:
                continue
            index = insertion_order[memory_id]
            lexical = self._lexical.score(query, memory_id) if memory_id in lexical_ids else 0.0
            asset_hits = len(assets.intersection(record.asset_ids))
            if lexical <= 0.0 and asset_hits == 0:
                continue                                   # no overlap, no recall
            base = lexical + _W_ASSET_HIT * asset_hits
            max_base = max(max_base, base)
            scored.append((base, index, _structural_prior(record) if use_structure else 0.0, record))

        # Structural rerank — a bounded fraction of the top lexical score, so it
        # reorders near-ties / applies priors but never overrides a clear winner.
        scale = _STRUCT_COEF * max_base
        ranked: dict[str, list[tuple[float, int, MemoryRecord]]] = {tier: [] for tier in _EMPTY_TIERS}
        for base, index, prior, record in scored:
            final = base + scale * prior
            ranked[record.tier].append((final, index, record))

        return {
            tier: [
                record
                for _, _, record in sorted(items, key=lambda item: (-item[0], item[1]))[:limit_per_tier]
            ]
            for tier, items in ranked.items()
        }
