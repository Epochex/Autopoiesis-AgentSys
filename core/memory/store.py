"""Tiered long/short-term memory store.

Quarantine, redaction, and purge have deliberately different meanings.
Quarantine removes a record from retrieval while retaining its content for
inspection. Redaction removes content from the current record and historical
event payloads while retaining a tombstone. Purge is an explicitly enabled
administrative action that removes the identity and events after an independent
immutable log entry is written.

Three learned tiers (episodic / semantic / procedural) plus seeded asset
profiles overlay into one queryable store.  The always-available route is
deterministic segmented BM25 plus exact asset and operational-entity identity.
An optional dense route adds semantic candidates from the immutable-base/Flat-
delta index; the memory records in this store remain the source of truth.

Retrieval ranking (see :meth:`TieredMemoryStore.retrieve`) is a two-stage
query-dependent-base-then-structural design:

  1. **Query-dependent base** — Okapi BM25 (IDF-weighted term frequency, see
     :mod:`core.memory.bm25`) over each record's full document (its text tokens
     plus its tag labels), exact-identity asset matches, and exact operational
     entity matches. The entity route keeps identifiers such as service units,
     interfaces, and error codes intact instead of depending on a language's
     word segmentation. BM25 replaces the earlier raw tag-overlap count, which
     under-ranked the truly relevant record whenever a common term matched many
     memories: on the LongMemEval-500 anchor, scoring the whole session text this
     way lifts the store's own recall@5 from 0.906 to 0.970 — matching the BM25
     lexical ceiling (0.970).
  2. **Structural rerank** — a bounded, deterministic prior built ONLY from the
     record's own lifecycle signals (tier, A-MEM link centrality, reflection
     importance, Ebbinghaus strength/recency, verified-reuse confidence). It is
     scaled to a small fraction of the top lexical score, so it reorders
     near-ties and expresses priors without overriding a clear lexical winner
     (RRF-style equal-vote fusion of a weak structural signal was measured to
     wreck precision; a bounded rerank does not). Toggle with ``use_structure``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, Field

from core.memory.bm25 import tokenize
from core.memory.segmented_bm25 import SegmentedBM25Index


MemoryTier = Literal["episodic", "semantic", "procedural", "asset_profile"]

# --- lexical base weights ---------------------------------------------------
# Shared assets are a strong structured identity signal (exact, not lexical);
# BM25 over the record's full text + tags carries the lexical relevance.
_W_ASSET_HIT = 2.0
# One exact operational entity gets the same 2.0 contribution as one exact asset.
# Both identify a concrete object, while counting each distinct entity only once
# prevents repetition across text, tags, and asset_ids from inflating its vote.
_W_ENTITY_HIT = 2.0
_GRAPH_HOP_DECAY = 0.35
_DENSE_ROUTE_COEF = 0.35

# These patterns deliberately cover identifiers that retain their meaning across
# languages. IPv4 octets stop at 255 and CIDR prefixes stop at 32, service units
# keep their punctuation, interface names follow the four Linux prefixes used by
# the domain, and error codes remain uppercase so ordinary prose is not promoted.
# Hostnames include dotted names and single labels with a digit or hyphen; the
# extra shape requirement keeps plain English query words on the unchanged BM25
# path. ASCII identifier boundaries prevent r230 from matching r2300 while still
# allowing an identifier to sit directly beside Chinese punctuation or text.
_IPV4_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?<![A-Za-z0-9_.@/-])(?:{_IPV4_OCTET}\.){{3}}{_IPV4_OCTET}/(?:[12]?\d|3[0-2])"
        r"(?![A-Za-z0-9_.@/-])"
    ),
    re.compile(
        rf"(?<![A-Za-z0-9_.@/-])(?:{_IPV4_OCTET}\.){{3}}{_IPV4_OCTET}"
        r"(?![A-Za-z0-9_.@/-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.@/-])"
        r"[A-Za-z0-9](?:[A-Za-z0-9_.@-]*[A-Za-z0-9@])?\.service"
        r"(?![A-Za-z0-9_.@/-])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.@/-])(?:eno|enp|eth|ens)\d[A-Za-z0-9_.-]*"
        r"(?![A-Za-z0-9_.@/-])",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])"),
    re.compile(
        r"(?<![A-Za-z0-9_.-])"
        r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.-])"
        r"(?=[A-Za-z0-9-]{2,63}(?![A-Za-z0-9_.-]))"
        r"(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*(?:\d|-))"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
        r"(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    ),
)

# --- structural rerank weights (Phase B memory dynamics) --------------------
# The structural prior is a convex blend (weights sum to 1) of five normalised
# lifecycle signals, then scaled by ``_STRUCT_COEF`` * (top base score) so it
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


class MemoryRelation(BaseModel):
    """Typed, evidence-carrying edge between two memory events.

    ``similar_to`` is associative, while temporal or causal relation types are
    only written when the source trace actually supports that meaning.
    """

    target_id: str
    relation_type: str
    confidence: float = 1.0
    evidence_ids: list[str] = Field(default_factory=list)


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
    # Longitudinal facts. These turn an associative memory family into an
    # inspectable event history without pretending similarity is causality.
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    # World time is separate from observation time. The half-open interval lets
    # a successor begin exactly when its predecessor stops being true while both
    # records remain available for reconstructing past beliefs.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    event_type: str | None = None
    relations: list[MemoryRelation] = Field(default_factory=list)
    config_version: str | None = None
    metric_window: dict[str, Any] = Field(default_factory=dict)
    baseline_delta: dict[str, float] = Field(default_factory=dict)
    # Set only on a tombstone. The reason is administrative erasure metadata,
    # not remembered content, and makes the retained identity chain explainable.
    redaction_reason: str | None = None
    redacted_at: datetime | None = None


class MemoryRepository(Protocol):
    """Durable source used only at offline consolidation boundaries."""

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]: ...

    def sync_records(
        self,
        records: Sequence[MemoryRecord],
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> list[Any]: ...

    def redact(
        self,
        memory_id: str,
        reason: str,
        *,
        expected_version: int | None = None,
    ) -> Any: ...

    def delete(
        self,
        memory_id: str,
        reason: str,
        *,
        expected_version: int | None = None,
    ) -> Any: ...

    def purge(
        self,
        memory_id: str,
        reason: str,
        *,
        actor: str,
        allow_purge: bool = False,
    ) -> Any: ...


class VectorMemoryProjection(Protocol):
    """Rebuildable semantic projection used by the online retrieval route."""

    def upsert(self, memory_id: str, text: str, **kwargs: Any) -> bool: ...

    def delete(self, memory_id: str, **kwargs: Any) -> bool: ...

    def search(self, query: str, k: int = 10) -> list[Any]: ...

    def compact(self) -> int: ...

    def should_compact(self) -> bool: ...

    def health(self) -> dict[str, Any]: ...


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


def _extract_query_entities(query_terms: Sequence[str]) -> list[str]:
    """Return distinct operational identifiers without tokenising prose.

    Matches retain the query's original spelling for diagnostics. Deduplication
    uses case folding because Linux units, hostnames, and error-code mentions are
    commonly cased differently by probes and operators but denote the same key.
    """
    entities: dict[str, str] = {}
    for term in query_terms:
        for pattern in _ENTITY_PATTERNS:
            for match in pattern.finditer(term):
                entity = match.group(0)
                entities.setdefault(entity.casefold(), entity)
    return list(entities.values())


def _literal_entity_hits(record: "MemoryRecord", entities: Sequence[str]) -> list[str]:
    """Find query entities as complete literals in text, tags, or asset ids."""
    fields = (record.text, *record.tags, *record.asset_ids)
    hits: list[str] = []
    for entity in entities:
        # Identifier boundaries are ASCII on purpose: Chinese text can touch an
        # entity without whitespace, while a longer ASCII identifier is not an
        # exact match. A final sentence period is allowed, while ``.suffix`` is
        # treated as part of a longer identifier. IGNORECASE handles casing
        # without changing the entity's punctuation.
        literal = re.compile(
            rf"(?<![A-Za-z0-9_@/-])(?<![A-Za-z0-9]\.){re.escape(entity)}"
            r"(?![A-Za-z0-9_@/-]|\.[A-Za-z0-9])",
            re.IGNORECASE,
        )
        if any(literal.search(field) for field in fields):
            hits.append(entity)
    return hits


def _utc_instant(value: datetime) -> datetime:
    """Normalise instants so persisted aware values and legacy naive UTC compare."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _valid_at(record: "MemoryRecord", instant: datetime) -> bool:
    """Whether a fact is true at an instant under [valid_from, valid_to)."""
    starts_before = (
        record.valid_from is None or _utc_instant(record.valid_from) <= instant
    )
    ends_after = record.valid_to is None or _utc_instant(record.valid_to) > instant
    return starts_before and ends_after


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
        vector_index: VectorMemoryProjection | None = None,
    ):
        self.enabled = enabled
        self._records: list[MemoryRecord] = []
        self._by_id: dict[str, MemoryRecord] = {}
        self._indexed_assets: dict[str, set[str]] = {}
        self._asset_to_ids: dict[str, set[str]] = {}
        self._repository = repository
        # PostgreSQL exposes versioned loads; simpler/offline repositories keep
        # the legacy full-snapshot protocol. Version-aware stores retain a
        # baseline payload so in-place model mutations can still be detected
        # without forcing unrelated records through the same CAS batch.
        self._repository_uses_versions = bool(
            repository is not None and hasattr(repository, "load_versioned_records")
        )
        self._repository_versions: dict[str, int] = {}
        self._repository_snapshots: dict[str, dict[str, Any]] = {}
        self._vector = vector_index
        self._vector_degraded_reason: str | None = None
        self._projection_lock = threading.RLock()
        self._projected_offset = 0
        self._projected_versions: dict[str, int] = {}
        self._last_retrieval_details: list[dict[str, Any]] = []
        self._lexical_options = {
            "seal_threshold": lexical_seal_threshold,
            "compact_segment_threshold": lexical_compact_segment_threshold,
        }
        self._lexical = SegmentedBM25Index(**self._lexical_options)

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
        load_versioned = getattr(repository, "load_versioned_records", None)
        if callable(load_versioned):
            loaded = load_versioned(include_quarantined=True)
            store.seed([record for record, _version in loaded])
            store._repository_versions = {
                record.memory_id: int(version) for record, version in loaded
            }
            store._repository_snapshots = {
                record.memory_id: store._snapshot(record) for record, _version in loaded
            }
        else:
            store.seed(repository.load_records(include_quarantined=True))
        return store

    def flush(self) -> list[Any]:
        """Atomically persist the current memory snapshot when a repository exists."""
        if self._repository is None:
            return []
        if not self._repository_uses_versions:
            return self._repository.sync_records(self.records())

        dirty = [
            record
            for record in self._records
            if self._repository_snapshots.get(record.memory_id) != self._snapshot(record)
        ]
        if not dirty:
            return []
        expected_versions = {
            record.memory_id: self._repository_versions.get(record.memory_id, 0)
            for record in dirty
        }
        writes = self._repository.sync_records(
            dirty,
            expected_versions=expected_versions,
        )
        for record, write in zip(dirty, writes, strict=True):
            version = getattr(write, "version", None)
            if version is None:
                raise TypeError("version-aware repository returned a write without version")
            self._repository_versions[record.memory_id] = int(version)
            self._repository_snapshots[record.memory_id] = self._snapshot(record)
        return writes

    def replace_records(self, records: Sequence[MemoryRecord]) -> None:
        """Atomically restore a complete snapshot and rebuild derived indexes.

        Used when a consolidation transaction fails after mutating the local
        working set. Dense old versions are tombstoned/overwritten and reclaimed
        by normal compaction; they never remain visible.
        """
        copies = [record.model_copy(deep=True) for record in records]
        if len({record.memory_id for record in copies}) != len(copies):
            raise ValueError("replacement snapshot contains duplicate memory ids")
        with self._projection_lock:
            if self._vector is not None:
                for record in self._records:
                    if not record.quarantined:
                        self._vector.delete(record.memory_id)
            self._records = []
            self._by_id = {}
            self._indexed_assets = {}
            self._asset_to_ids = {}
            self._lexical = SegmentedBM25Index(**self._lexical_options)
            for record in copies:
                self.add(record)

    def reload_from_repository(self) -> None:
        """Discard local uncommitted mutations and reload the durable truth."""
        if self._repository is None:
            raise RuntimeError("memory store has no durable repository")
        load_versioned = getattr(self._repository, "load_versioned_records", None)
        if callable(load_versioned):
            loaded = load_versioned(include_quarantined=True)
            self.replace_records([record for record, _version in loaded])
            self._repository_versions = {
                record.memory_id: int(version) for record, version in loaded
            }
            self._repository_snapshots = {
                record.memory_id: self._snapshot(record) for record, _version in loaded
            }
            self._projected_versions.update(self._repository_versions)
        else:
            self.replace_records(
                self._repository.load_records(include_quarantined=True)
            )

    @staticmethod
    def _snapshot(record: MemoryRecord) -> dict[str, Any]:
        """Detached JSON-compatible payload used for reliable dirty detection."""
        return record.model_dump(mode="json")

    @staticmethod
    def _lexical_tokens(record: MemoryRecord) -> list[str]:
        return tokenize(record.text) + [tag.lower() for tag in record.tags]

    @staticmethod
    def vector_document(record: MemoryRecord) -> str:
        """Stable text projected into the dense index for one memory version."""
        parts = [record.text, *record.tags, *record.asset_ids]
        return "\n".join(part for part in parts if part)

    def attach_vector_index(self, vector_index: VectorMemoryProjection) -> None:
        """Attach an already-built dense projection.

        Building is intentionally kept outside the store so startup can create
        one generation from the complete durable snapshot instead of appending
        every restored record to the mutable delta.
        """
        self._vector = vector_index
        self._vector_degraded_reason = None

    def mark_vector_degraded(self, reason: str) -> None:
        """Expose an explicit sparse-only state after optional dense startup fails."""
        self._vector = None
        self._vector_degraded_reason = reason

    @property
    def projected_offset(self) -> int:
        """Highest durable event offset installed by an index projector."""
        with self._projection_lock:
            return self._projected_offset

    @property
    def repository(self) -> MemoryRepository | None:
        """Durable source attached to this store, when persistence is enabled."""
        return self._repository

    def prime_projection(self, event_offset: int) -> None:
        """Align a snapshot-built store with an existing consumer checkpoint."""
        if isinstance(event_offset, bool) or not isinstance(event_offset, int):
            raise TypeError("event_offset must be an integer")
        if event_offset < 0:
            raise ValueError("event_offset must be non-negative")
        with self._projection_lock:
            if event_offset < self._projected_offset:
                raise ValueError("projection offset cannot move backwards")
            self._projected_offset = event_offset

    def apply_index_event(
        self,
        record: MemoryRecord,
        *,
        event_type: Literal["UPSERT", "QUARANTINE", "REDACT"],
        event_offset: int,
        version: int,
    ) -> bool:
        """Apply one ordered source event to the snapshot, BM25, assets and HNSW.

        Source offsets are tracked here rather than copied into each index's
        private generation clock: a process may rebuild those indexes from a
        newer PostgreSQL snapshot before resuming its consumer checkpoint.
        Retrying an already installed event is therefore a no-op.
        """
        if event_type not in {"UPSERT", "QUARANTINE", "REDACT"}:
            raise ValueError(f"unknown memory event type: {event_type}")
        if isinstance(event_offset, bool) or not isinstance(event_offset, int):
            raise TypeError("event_offset must be an integer")
        if event_offset <= 0:
            raise ValueError("event_offset must be positive")
        if isinstance(version, bool) or not isinstance(version, int):
            raise TypeError("version must be an integer")
        if version <= 0:
            raise ValueError("version must be positive")
        if event_type in {"QUARANTINE", "REDACT"} and not record.quarantined:
            raise ValueError(f"{event_type} event must carry a quarantined snapshot")

        with self._projection_lock:
            if event_offset <= self._projected_offset:
                return False
            current_version = max(
                self._projected_versions.get(record.memory_id, 0),
                self._repository_versions.get(record.memory_id, 0),
            )
            if version <= current_version:
                self._projected_offset = event_offset
                return False

            projected = record.model_copy(deep=True)
            if event_type in {"QUARANTINE", "REDACT"}:
                projected.quarantined = True

            # Embedding is the fallible part, so finish it before replacing the
            # authoritative in-memory record. If a process dies after the vector
            # append, its version filter hides the extra physical version on retry.
            if self._vector is not None:
                if projected.quarantined:
                    self._vector.delete(
                        projected.memory_id,
                        offset=event_offset,
                        version=version,
                    )
                else:
                    self._vector.upsert(
                        projected.memory_id,
                        self.vector_document(projected),
                        offset=event_offset,
                        version=version,
                    )

            existing = self._by_id.get(projected.memory_id)
            if existing is not None:
                self._unindex_assets(projected.memory_id)
                position = self._records.index(existing)
                self._records[position] = projected
            else:
                self._records.append(projected)
            self._by_id[projected.memory_id] = projected

            if projected.quarantined:
                self._lexical.delete(projected.memory_id)
            else:
                assets = set(projected.asset_ids)
                self._indexed_assets[projected.memory_id] = assets
                for asset_id in assets:
                    self._asset_to_ids.setdefault(asset_id, set()).add(projected.memory_id)
                self._lexical.upsert(
                    projected.memory_id,
                    self._lexical_tokens(projected),
                )

            self._projected_versions[projected.memory_id] = version
            if self._repository_uses_versions:
                self._repository_versions[projected.memory_id] = version
                self._repository_snapshots[projected.memory_id] = self._snapshot(projected)
            self._projected_offset = event_offset
            return True

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
            if self._vector is not None:
                self._vector.delete(memory_id)
            return True
        assets = set(record.asset_ids)
        self._indexed_assets[memory_id] = assets
        for asset_id in assets:
            self._asset_to_ids.setdefault(asset_id, set()).add(memory_id)
        self._lexical.upsert(memory_id, self._lexical_tokens(record))
        if self._vector is not None:
            self._vector.upsert(memory_id, self.vector_document(record))
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
        """Exclude an untrusted record from retrieval while retaining its content."""
        record = self._by_id.get(memory_id)
        if record is not None:
            was_active = not record.quarantined
            record.quarantined = True
            record.tags.append(f"quarantine:{reason}")
            if was_active:
                self._unindex_assets(memory_id)
                self._lexical.delete(memory_id)
                if self._vector is not None:
                    self._vector.delete(memory_id)

    def restore(self, memory_id: str, reason: str) -> bool:
        """Return a capacity-retired record to retrieval after fresh verification.

        Restoration is deliberately reason-bound.  A new externally verified case
        can make a record retired as ``forgotten`` useful again, while a record
        quarantined for contradiction, supersession, or administrative redaction
        stays excluded until a separate reviewed workflow handles it.
        """
        record = self._by_id.get(memory_id)
        marker = f"quarantine:{reason.strip()}"
        if (
            record is None
            or not record.quarantined
            or not reason.strip()
            or marker not in record.tags
            or record.redacted_at is not None
            or record.redaction_reason is not None
        ):
            return False
        other_quarantines = [
            tag for tag in record.tags
            if tag.startswith("quarantine:") and tag != marker
        ]
        if other_quarantines:
            return False
        record.tags = [tag for tag in record.tags if tag != marker]
        record.quarantined = False
        self.reindex(memory_id)
        return True

    @staticmethod
    def _redacted_record(record: MemoryRecord, reason: str) -> MemoryRecord:
        """Build a content-free tombstone while preserving identity and timestamps."""
        tombstone = record.model_copy(deep=True)
        tombstone.text = "[REDACTED]"
        tombstone.tags = []
        tombstone.asset_ids = []
        tombstone.evidence_ids = []
        tombstone.source_trace_ids = []
        tombstone.evidence_snapshot = []
        tombstone.links = []
        tombstone.relations = []
        tombstone.metric_window = {}
        tombstone.baseline_delta = {}
        tombstone.confidence = 0.0
        tombstone.importance = 0.0
        tombstone.strength = 0.0
        tombstone.access_count = 0
        tombstone.superseded_by = None
        tombstone.event_type = "REDACT"
        tombstone.config_version = None
        tombstone.quarantined = True
        tombstone.redaction_reason = reason
        tombstone.redacted_at = datetime.now(timezone.utc)
        return tombstone

    def _replace_with_tombstone(self, memory_id: str, tombstone: MemoryRecord) -> None:
        existing = self._by_id[memory_id]
        self._unindex_assets(memory_id)
        self._lexical.delete(memory_id)
        if self._vector is not None:
            self._vector.delete(memory_id)
        position = self._records.index(existing)
        self._records[position] = tombstone
        self._by_id[memory_id] = tombstone

    def redact(self, memory_id: str, reason: str) -> bool:
        """Erase content, keep a tombstone, and remove all retrieval projections.

        Redaction is stronger than quarantine because original content must no
        longer exist, including in historical events. It is weaker than purge
        because the id, tier, timestamps, and event chain remain.
        """
        reason = reason.strip()
        if not reason:
            raise ValueError("redaction reason must not be empty")
        record = self._by_id.get(memory_id)
        if record is None:
            return False

        tombstone = self._redacted_record(record, reason)
        if self._repository is not None:
            redact = getattr(self._repository, "redact", None)
            get_record = getattr(self._repository, "get", None)
            if not callable(redact) or not callable(get_record):
                raise RuntimeError("durable repository does not support redaction")
            expected_version = self._repository_versions.get(memory_id)
            write = redact(memory_id, reason, expected_version=expected_version)
            loaded = get_record(memory_id)
            if loaded is None:
                raise RuntimeError("redaction committed without a tombstone")
            tombstone, version = loaded
            self._repository_versions[memory_id] = int(version)
            self._repository_snapshots[memory_id] = self._snapshot(tombstone)
            event_offset = getattr(write, "event_offset", None)
            if event_offset is not None:
                self._projected_versions[memory_id] = int(version)

        self._replace_with_tombstone(memory_id, tombstone)
        return True

    def delete(self, memory_id: str, reason: str) -> bool:
        """Default deletion protocol: redact content and retain a tombstone."""
        return self.redact(memory_id, reason)

    def purge(
        self,
        memory_id: str,
        reason: str,
        *,
        actor: str,
        allow_purge: bool = False,
    ) -> bool:
        """Explicitly remove an identity after its durable purge log is written."""
        record = self._by_id.get(memory_id)
        if record is None:
            return False
        if self._repository is None:
            raise RuntimeError("purge requires a durable repository and immutable log")
        purge = getattr(self._repository, "purge", None)
        if not callable(purge):
            raise RuntimeError("durable repository does not support purge")
        purge(memory_id, reason, actor=actor, allow_purge=allow_purge)

        self._unindex_assets(memory_id)
        self._lexical.delete(memory_id)
        if self._vector is not None:
            self._vector.delete(memory_id)
        self._records.remove(record)
        del self._by_id[memory_id]
        self._repository_versions.pop(memory_id, None)
        self._repository_snapshots.pop(memory_id, None)
        self._projected_versions.pop(memory_id, None)
        return True

    def index_health(self) -> dict[str, Any]:
        """Expose sparse and optional dense projection lifecycle state."""
        health: dict[str, Any] = self._lexical.health()
        health["vector_enabled"] = self._vector is not None
        health["vector_degraded"] = self._vector_degraded_reason is not None
        health["vector_degraded_reason"] = self._vector_degraded_reason
        health["vector"] = self._vector.health() if self._vector is not None else None
        return health

    def compact_index(self, *, force: bool = False) -> bool:
        """Compact obsolete lexical versions without changing memory history."""
        return self._lexical.compact(force=force)

    def vector_index_should_compact(self) -> bool:
        return bool(self._vector is not None and self._vector.should_compact())

    def compact_vector_index(self) -> int:
        if self._vector is None:
            return 0
        return self._vector.compact()

    def retrieve(
        self,
        query_terms: list[str],
        asset_ids: list[str],
        limit_per_tier: int = 3,
        *,
        use_structure: bool = True,
        as_of: datetime | None = None,
        graph_depth: int = 0,
        graph_candidate_limit: int = 48,
    ) -> dict[str, list[MemoryRecord]]:
        """Top ``limit_per_tier`` active records per tier for these terms/assets.

        Without a vector projection, a record must overlap at least one BM25
        term, exact asset, or exact operational entity. With it enabled, positive
        dense hits may introduce semantic candidates. Candidates are ordered by
        bounded hybrid score and structural prior; ties break on insertion order.

        ``as_of`` reconstructs the facts valid at one world-time instant. Its
        default is the current time; records created before validity tracking
        have two open bounds and therefore retain their previous behaviour.

        ``use_structure=False`` returns the query-dependent base ranking, used
        to isolate the structural rerank's contribution. It is the BM25-only
        floor when no asset or operational entity matches.
        """
        if not self.enabled or limit_per_tier <= 0:
            self._last_retrieval_details = []
            return {tier: [] for tier in _EMPTY_TIERS}
        if graph_depth < 0:
            raise ValueError("graph_depth must be non-negative")
        if graph_candidate_limit < 1:
            raise ValueError("graph_candidate_limit must be positive")

        effective_at = _utc_instant(as_of or datetime.now(timezone.utc))
        query = [term.lower() for term in query_terms if term]
        query_entities = _extract_query_entities(query_terms)
        assets = set(asset_ids)
        insertion_order = {record.memory_id: index for index, record in enumerate(self._records)}
        lexical_ids = {doc_id for doc_id, _ in self._lexical.rank_with_scores(query)}
        asset_candidate_ids = {
            memory_id
            for asset_id in assets
            for memory_id in self._asset_to_ids.get(asset_id, ())
        }
        entity_hits_by_id = {
            record.memory_id: hits
            for record in self._records
            if not record.quarantined and _valid_at(record, effective_at)
            if (hits := _literal_entity_hits(record, query_entities))
        }
        seed_ids = lexical_ids | asset_candidate_ids | set(entity_hits_by_id)
        details: dict[str, dict[str, Any]] = {}

        dense_scores: dict[str, float] = {}
        if self._vector is not None and (query or assets):
            query_text = " ".join([*query, *sorted(assets)])
            dense_k = max(32, limit_per_tier * len(_EMPTY_TIERS) * 4)
            for hit in self._vector.search(query_text, k=dense_k):
                memory_id = str(hit.memory_id)
                if (
                    memory_id in self._by_id
                    and _valid_at(self._by_id[memory_id], effective_at)
                    and float(hit.score) > 0.0
                ):
                    dense_scores[memory_id] = max(dense_scores.get(memory_id, 0.0), float(hit.score))
            seed_ids.update(dense_scores)

        # First-stage lexical, exact-asset, and exact-entity scores seed a bounded
        # graph walk.
        # Links therefore retrieve related incidents instead of merely increasing
        # one record's centrality scalar. Each hop is discounted and the frontier
        # is capped, so a highly connected family cannot flood the context.
        candidate_scores: dict[str, float] = {}
        for memory_id in seed_ids:
            record = self._by_id[memory_id]
            if record.quarantined or not _valid_at(record, effective_at):
                continue
            lexical = self._lexical.score(query, memory_id) if memory_id in lexical_ids else 0.0
            asset_hits = len(assets.intersection(record.asset_ids))
            entity_hits = entity_hits_by_id.get(memory_id, [])
            entity_score = _W_ENTITY_HIT * len(entity_hits)
            base = lexical + _W_ASSET_HIT * asset_hits + entity_score
            if base > 0.0:
                candidate_scores[memory_id] = base
                details[memory_id] = {
                    "memory_id": memory_id,
                    "tier": record.tier,
                    "lexical_score": round(lexical, 6),
                    "asset_hits": asset_hits,
                    "entity_hits": list(entity_hits),
                    "entity_score": round(entity_score, 6),
                    "vector_score": round(dense_scores.get(memory_id, 0.0), 6),
                    "graph_hop": 0,
                    "graph_parent_id": None,
                }

        # Dense similarity is deliberately bounded relative to the strongest
        # lexical or exact-identity signal. It can recall a semantic-only
        # candidate, but it cannot swamp an exact asset or entity match.
        dense_scale = _DENSE_ROUTE_COEF * max(max(candidate_scores.values(), default=0.0), 1.0)
        for memory_id, dense_score in dense_scores.items():
            record = self._by_id[memory_id]
            if not record.quarantined and _valid_at(record, effective_at):
                candidate_scores[memory_id] = candidate_scores.get(memory_id, 0.0) + dense_scale * dense_score
                details.setdefault(
                    memory_id,
                    {
                        "memory_id": memory_id,
                        "tier": record.tier,
                        "lexical_score": 0.0,
                        "asset_hits": 0,
                        "entity_hits": [],
                        "entity_score": 0.0,
                        "graph_hop": 0,
                        "graph_parent_id": None,
                    },
                )["vector_score"] = round(dense_score, 6)

        frontier = dict(candidate_scores)
        for hop_index in range(graph_depth):
            expanded: dict[str, float] = {}
            for source_id, source_score in sorted(
                frontier.items(), key=lambda item: (-item[1], item[0])
            )[:graph_candidate_limit]:
                source = self._by_id.get(source_id)
                if (
                    source is None
                    or source.quarantined
                    or not _valid_at(source, effective_at)
                ):
                    continue
                propagated = source_score * _GRAPH_HOP_DECAY
                for linked_id in source.links:
                    linked = self._by_id.get(linked_id)
                    if (
                        linked is None
                        or linked.quarantined
                        or not _valid_at(linked, effective_at)
                    ):
                        continue
                    if propagated > candidate_scores.get(linked_id, 0.0):
                        candidate_scores[linked_id] = propagated
                        expanded[linked_id] = max(expanded.get(linked_id, 0.0), propagated)
                        existing_hop = details.get(linked_id, {}).get("graph_hop")
                        if existing_hop in (None, 0) and linked_id not in seed_ids:
                            details[linked_id] = {
                                "memory_id": linked_id,
                                "tier": linked.tier,
                                "lexical_score": 0.0,
                                "asset_hits": 0,
                                "entity_hits": [],
                                "entity_score": 0.0,
                                "vector_score": 0.0,
                                "graph_hop": hop_index + 1,
                                "graph_parent_id": source_id,
                            }
            frontier = dict(
                sorted(expanded.items(), key=lambda item: (-item[1], item[0]))[
                    :graph_candidate_limit
                ]
            )
            if not frontier:
                break

        scored: list[tuple[float, int, float, MemoryRecord]] = []  # (base, index, struct_prior, rec)
        max_base = 0.0
        for memory_id, base in candidate_scores.items():
            record = self._by_id[memory_id]
            if record.quarantined or not _valid_at(record, effective_at):
                continue
            index = insertion_order[memory_id]
            max_base = max(max_base, base)
            scored.append((base, index, _structural_prior(record) if use_structure else 0.0, record))

        # Structural rerank — a bounded fraction of the top query-dependent base,
        # so it reorders near-ties / applies priors but never overrides a clear
        # winner.
        scale = _STRUCT_COEF * max_base
        ranked: dict[str, list[tuple[float, int, MemoryRecord]]] = {tier: [] for tier in _EMPTY_TIERS}
        for base, index, prior, record in scored:
            final = base + scale * prior
            ranked[record.tier].append((final, index, record))
            details[record.memory_id]["structural_prior"] = round(prior, 6)
            details[record.memory_id]["final_score"] = round(final, 6)

        self._last_retrieval_details = sorted(
            details.values(),
            key=lambda item: (-float(item.get("final_score", 0.0)), item["memory_id"]),
        )

        return {
            tier: [
                record
                for _, _, record in sorted(items, key=lambda item: (-item[0], item[1]))[:limit_per_tier]
            ]
            for tier, items in ranked.items()
        }

    def retrieval_diagnostics(self) -> list[dict[str, Any]]:
        """Detached source/score trace for the most recent retrieval."""
        return [dict(item) for item in self._last_retrieval_details]
