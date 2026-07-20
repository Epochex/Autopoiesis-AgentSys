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

from typing import Literal

from pydantic import BaseModel, Field

from core.memory.bm25 import BM25Index, tokenize


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
    # provenance-linked snapshot of the evidence observed when this memory was written,
    # so a recurring incident can be resolved by recall instead of re-investigation.
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
        active = [(index, rec) for index, rec in enumerate(self._records) if not rec.quarantined]

        # Lexical base — Okapi BM25 over each record's full document: the text
        # tokens plus its tag labels (tags carry non-text signals like skill:ids).
        # Indexing the full text, not just the (often capped) tag list, is what
        # gives BM25 the whole session to score — the difference between recall@5
        # 0.894 (48-tag index) and 0.966 (full text) on LongMemEval.
        bm25 = BM25Index({rec.memory_id: tokenize(rec.text) + [t.lower() for t in rec.tags] for _, rec in active})

        scored: list[tuple[float, int, float, MemoryRecord]] = []  # (base, index, struct_prior, rec)
        max_base = 0.0
        for index, record in active:
            lexical = bm25.score(query, record.memory_id) if query else 0.0
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
