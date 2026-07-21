"""Incremental, durable Okapi BM25 over immutable segments.

The index keeps recent mutations in a small mutable delta.  Sealing turns the
delta into an immutable inverted-index segment; compaction rewrites all live
documents into one segment and drops superseded versions and tombstones.

Scores deliberately use corpus-wide ``N``, document frequency and average
document length.  Segment boundaries therefore do not change BM25 semantics:
the live view ranks identically to :class:`core.memory.bm25.BM25Index`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from core.memory.bm25 import tokenize


_SCHEMA_VERSION = 1


class SnapshotCorruptionError(ValueError):
    """Raised when a persisted index snapshot is malformed or fails checksum."""


@dataclass(frozen=True, slots=True)
class _DocumentVersion:
    doc_id: str
    tokens: tuple[str, ...]
    version: int
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class _Segment:
    """A sealed collection whose documents and posting lists never mutate."""

    documents: tuple[_DocumentVersion, ...]
    postings: Mapping[str, tuple[_DocumentVersion, ...]]

    @classmethod
    def build(cls, documents: Iterable[_DocumentVersion]) -> "_Segment":
        docs = tuple(sorted(documents, key=lambda item: (item.doc_id, item.version)))
        mutable: dict[str, list[_DocumentVersion]] = {}
        for doc in docs:
            if doc.deleted:
                continue
            for term in set(doc.tokens):
                mutable.setdefault(term, []).append(doc)
        postings = MappingProxyType({term: tuple(entries) for term, entries in mutable.items()})
        return cls(documents=docs, postings=postings)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class SegmentedBM25Index:
    """Thread-safe incremental BM25 index with durable generation snapshots.

    ``upsert`` and ``delete`` accept an optional monotonically increasing event
    offset.  Supplying offsets lets a caller checkpoint an event stream and
    resume from ``applied_offset`` after restart.  Without one, the index assigns
    the next local offset.
    """

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        seal_threshold: int = 1_000,
        compact_segment_threshold: int = 8,
        obsolete_ratio_threshold: float = 0.20,
        min_compaction_entries: int = 1_000,
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("b must be between 0 and 1")
        if seal_threshold < 1:
            raise ValueError("seal_threshold must be positive")
        if compact_segment_threshold < 2:
            raise ValueError("compact_segment_threshold must be at least 2")
        if not 0 <= obsolete_ratio_threshold <= 1:
            raise ValueError("obsolete_ratio_threshold must be between 0 and 1")
        if min_compaction_entries < 1:
            raise ValueError("min_compaction_entries must be positive")

        self.k1 = float(k1)
        self.b = float(b)
        self.seal_threshold = int(seal_threshold)
        self.compact_segment_threshold = int(compact_segment_threshold)
        self.obsolete_ratio_threshold = float(obsolete_ratio_threshold)
        self.min_compaction_entries = int(min_compaction_entries)
        self._segments: tuple[_Segment, ...] = ()
        self._delta: dict[str, _DocumentVersion] = {}
        self._latest: dict[str, _DocumentVersion] = {}
        self._document_frequency: Counter[str] = Counter()
        self._live_document_count = 0
        self._total_document_length = 0
        self._version_clock = 0
        self._generation = 0
        self._applied_offset = -1
        self._lock = threading.RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def applied_offset(self) -> int:
        with self._lock:
            return self._applied_offset

    def _take_offset_locked(self, offset: int | None) -> int:
        candidate = self._applied_offset + 1 if offset is None else offset
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise TypeError("offset must be an integer")
        if candidate <= self._applied_offset:
            raise ValueError(
                f"offset {candidate} is not newer than applied_offset {self._applied_offset}"
            )
        return candidate

    @staticmethod
    def _normalise_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
        if isinstance(tokens, str):
            raise TypeError("tokens must be an iterable of terms, not raw text")
        result = tuple(tokens)
        if any(not isinstance(term, str) for term in result):
            raise TypeError("tokens must contain only strings")
        return result

    def _remove_live_stats_locked(self, doc: _DocumentVersion | None) -> None:
        if doc is None or doc.deleted:
            return
        self._live_document_count -= 1
        self._total_document_length -= len(doc.tokens)
        for term in set(doc.tokens):
            self._document_frequency[term] -= 1
            if self._document_frequency[term] == 0:
                del self._document_frequency[term]

    def _add_live_stats_locked(self, doc: _DocumentVersion) -> None:
        if doc.deleted:
            return
        self._live_document_count += 1
        self._total_document_length += len(doc.tokens)
        self._document_frequency.update(set(doc.tokens))

    def _install_mutation_locked(self, doc: _DocumentVersion, offset: int) -> None:
        self._remove_live_stats_locked(self._latest.get(doc.doc_id))
        self._latest[doc.doc_id] = doc
        self._delta[doc.doc_id] = doc
        self._add_live_stats_locked(doc)
        self._applied_offset = offset
        self._generation += 1
        if len(self._delta) >= self.seal_threshold:
            self._seal_locked()

    def upsert(self, doc_id: str, tokens: Iterable[str], offset: int | None = None) -> None:
        """Insert or replace a document in the hot delta."""
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        normalised = self._normalise_tokens(tokens)
        with self._lock:
            accepted_offset = self._take_offset_locked(offset)
            self._version_clock += 1
            self._install_mutation_locked(
                _DocumentVersion(doc_id, normalised, self._version_clock), accepted_offset
            )

    def delete(self, doc_id: str, offset: int | None = None) -> bool:
        """Write a tombstone; return whether the document was live beforehand."""
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        with self._lock:
            accepted_offset = self._take_offset_locked(offset)
            existed = (current := self._latest.get(doc_id)) is not None and not current.deleted
            self._version_clock += 1
            self._install_mutation_locked(
                _DocumentVersion(doc_id, (), self._version_clock, deleted=True), accepted_offset
            )
            return existed

    def _seal_locked(self) -> bool:
        if not self._delta:
            return False
        self._segments = self._segments + (_Segment.build(self._delta.values()),)
        self._delta = {}
        return True

    def seal(self) -> bool:
        """Seal the current delta.  Returns ``False`` when it is empty."""
        with self._lock:
            return self._seal_locked()

    def maybe_seal(self) -> bool:
        """Seal only when the configured delta threshold has been reached."""
        with self._lock:
            if len(self._delta) < self.seal_threshold:
                return False
            return self._seal_locked()

    def _growth_locked(self) -> tuple[int, int, float]:
        physical = sum(len(segment.documents) for segment in self._segments) + len(self._delta)
        obsolete = 0
        for segment in self._segments:
            for doc in segment.documents:
                if self._latest.get(doc.doc_id) is not doc or doc.deleted:
                    obsolete += 1
        for doc in self._delta.values():
            if self._latest.get(doc.doc_id) is not doc or doc.deleted:
                obsolete += 1
        ratio = obsolete / physical if physical else 0.0
        return physical, obsolete, ratio

    def _compaction_due_locked(self) -> bool:
        physical, _, obsolete_ratio = self._growth_locked()
        return len(self._segments) >= self.compact_segment_threshold or (
            physical >= self.min_compaction_entries
            and obsolete_ratio >= self.obsolete_ratio_threshold
        )

    def compact(self, *, force: bool = False) -> bool:
        """Build a compact segment off-lock and atomically install it.

        A concurrent mutation invalidates the captured view; the candidate is
        discarded and a later maintenance pass retries. Readers and writers are
        therefore not blocked for the duration of the full rebuild.
        """
        with self._lock:
            if not force and not self._compaction_due_locked():
                return False
            captured_clock = self._version_clock
            live = tuple(doc for doc in self._latest.values() if not doc.deleted)

        replacement = _Segment.build(live) if live else None

        with self._lock:
            if captured_clock != self._version_clock:
                return False
            self._segments = (replacement,) if replacement is not None else ()
            self._delta = {}
            # The source event stream retains audit history. Once every old
            # segment is unreachable, keeping delete markers here is only bloat.
            self._latest = {doc.doc_id: doc for doc in live}
            self._generation += 1
            return True

    def maybe_compact(self) -> bool:
        """Compact when either the segment-count or obsolete-ratio policy is due."""
        return self.compact(force=False)

    def _candidate_documents_locked(self, query_tokens: Iterable[str]) -> dict[str, _DocumentVersion]:
        terms = set(query_tokens)
        candidates: dict[str, _DocumentVersion] = {}
        for segment in self._segments:
            for term in terms:
                for doc in segment.postings.get(term, ()):
                    latest = self._latest.get(doc.doc_id)
                    if latest is doc and not doc.deleted:
                        candidates[doc.doc_id] = doc
        for doc in self._delta.values():
            if not doc.deleted and terms.intersection(doc.tokens):
                latest = self._latest.get(doc.doc_id)
                if latest is doc:
                    candidates[doc.doc_id] = doc
        return candidates

    def _score_locked(self, query_tokens: tuple[str, ...], doc: _DocumentVersion) -> float:
        frequencies = Counter(doc.tokens)
        average_length = (
            self._total_document_length / self._live_document_count
            if self._live_document_count
            else 0.0
        )
        length_factor = self.k1 * (
            1 - self.b
            + self.b * (len(doc.tokens) / average_length if average_length else 0.0)
        )
        total = 0.0
        for term in query_tokens:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            document_frequency = self._document_frequency.get(term, 0)
            inverse_document_frequency = math.log(
                1
                + (self._live_document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            total += (
                inverse_document_frequency
                * (frequency * (self.k1 + 1))
                / (frequency + length_factor)
            )
        return total

    def score(self, query_tokens: Iterable[str], doc_id: str) -> float:
        """Return a live document's score, or raise ``KeyError`` if it is absent."""
        terms = tuple(query_tokens)
        with self._lock:
            doc = self._latest.get(doc_id)
            if doc is None or doc.deleted:
                raise KeyError(doc_id)
            return self._score_locked(terms, doc)

    def rank(self, query: str | Iterable[str], k: int | None = None) -> list[str]:
        return [doc_id for doc_id, _ in self.rank_with_scores(query, k)]

    def rank_with_scores(
        self,
        query: str | Iterable[str],
        k: int | None = None,
        *,
        query_tokens: Iterable[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Rank tokenised or raw-text queries; ties and rounding match ``BM25Index``."""
        if k is not None and k <= 0:
            return []
        if query_tokens is not None:
            terms = tuple(query_tokens)
        else:
            terms = tuple(tokenize(query)) if isinstance(query, str) else tuple(query)
        if not terms:
            return []
        with self._lock:
            candidates = self._candidate_documents_locked(terms)
            scored = [(self._score_locked(terms, doc), doc_id) for doc_id, doc in candidates.items()]
            limit = self._live_document_count if k is None else k
        scored = [(score, doc_id) for score, doc_id in scored if score > 0.0]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(doc_id, round(score, 6)) for score, doc_id in scored[:limit]]

    def health(self) -> dict[str, int | float | bool]:
        """Return lifecycle and bloat signals suitable for health reporting."""
        with self._lock:
            physical, obsolete, obsolete_ratio = self._growth_locked()
            return {
                "generation": self._generation,
                "applied_offset": self._applied_offset,
                "live_documents": self._live_document_count,
                "physical_entries": physical,
                "obsolete_entries": obsolete,
                "obsolete_ratio": obsolete_ratio,
                "segment_count": len(self._segments),
                "delta_entries": len(self._delta),
                "vocabulary_size": len(self._document_frequency),
                "average_document_length": (
                    self._total_document_length / self._live_document_count
                    if self._live_document_count
                    else 0.0
                ),
                "compaction_due": self._compaction_due_locked(),
            }

    @staticmethod
    def _encode_document(doc: _DocumentVersion) -> dict[str, object]:
        return {
            "doc_id": doc.doc_id,
            "tokens": list(doc.tokens),
            "version": doc.version,
            "deleted": doc.deleted,
        }

    def _state_locked(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "generation": self._generation,
            "applied_offset": self._applied_offset,
            "version_clock": self._version_clock,
            "config": {
                "k1": self.k1,
                "b": self.b,
                "seal_threshold": self.seal_threshold,
                "compact_segment_threshold": self.compact_segment_threshold,
                "obsolete_ratio_threshold": self.obsolete_ratio_threshold,
                "min_compaction_entries": self.min_compaction_entries,
            },
            "segments": [
                [self._encode_document(doc) for doc in segment.documents]
                for segment in self._segments
            ],
            "delta": [self._encode_document(doc) for doc in self._delta.values()],
        }

    def save(self, path: str | os.PathLike[str]) -> str:
        """Atomically persist a checksummed JSON snapshot and return its digest."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            state = self._state_locked()
        digest = hashlib.sha256(_canonical_json(state)).hexdigest()
        encoded = _canonical_json({"checksum": digest, "state": state}) + b"\n"

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
            ) as handle:
                temporary_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return digest

    snapshot = save

    @staticmethod
    def _decode_document(raw: object) -> _DocumentVersion:
        if not isinstance(raw, dict):
            raise SnapshotCorruptionError("snapshot document is not an object")
        try:
            doc_id = raw["doc_id"]
            tokens = raw["tokens"]
            version = raw["version"]
            deleted = raw["deleted"]
        except KeyError as exc:
            raise SnapshotCorruptionError(f"snapshot document missing {exc.args[0]}") from exc
        if (
            not isinstance(doc_id, str)
            or not doc_id
            or not isinstance(tokens, list)
            or any(not isinstance(term, str) for term in tokens)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(deleted, bool)
        ):
            raise SnapshotCorruptionError("invalid snapshot document")
        return _DocumentVersion(doc_id, tuple(tokens), version, deleted)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "SegmentedBM25Index":
        """Load and verify a snapshot, rebuilding all derived global statistics."""
        try:
            envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptionError(f"cannot read index snapshot: {exc}") from exc
        if not isinstance(envelope, dict) or set(envelope) != {"checksum", "state"}:
            raise SnapshotCorruptionError("invalid snapshot envelope")
        checksum = envelope["checksum"]
        state = envelope["state"]
        if not isinstance(checksum, str) or not isinstance(state, dict):
            raise SnapshotCorruptionError("invalid snapshot checksum or state")
        actual = hashlib.sha256(_canonical_json(state)).hexdigest()
        if not hmac.compare_digest(checksum, actual):
            raise SnapshotCorruptionError("snapshot checksum mismatch")
        if state.get("schema_version") != _SCHEMA_VERSION:
            raise SnapshotCorruptionError("unsupported snapshot schema")

        try:
            config = state["config"]
            if not isinstance(config, dict):
                raise TypeError("config")
            index = cls(
                k1=config["k1"],
                b=config["b"],
                seal_threshold=config["seal_threshold"],
                compact_segment_threshold=config["compact_segment_threshold"],
                obsolete_ratio_threshold=config["obsolete_ratio_threshold"],
                min_compaction_entries=config["min_compaction_entries"],
            )
            generation = state["generation"]
            applied_offset = state["applied_offset"]
            version_clock = state["version_clock"]
            raw_segments = state["segments"]
            raw_delta = state["delta"]
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
                or isinstance(applied_offset, bool)
                or not isinstance(applied_offset, int)
                or applied_offset < -1
                or isinstance(version_clock, bool)
                or not isinstance(version_clock, int)
                or version_clock < 0
                or not isinstance(raw_segments, list)
                or not isinstance(raw_delta, list)
                or any(not isinstance(raw_segment, list) for raw_segment in raw_segments)
            ):
                raise TypeError("state")
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotCorruptionError(f"invalid snapshot state: {exc}") from exc

        segments = tuple(
            _Segment.build(cls._decode_document(raw) for raw in raw_segment)
            for raw_segment in raw_segments
        )
        delta_docs = [cls._decode_document(raw) for raw in raw_delta]
        delta: dict[str, _DocumentVersion] = {}
        for doc in delta_docs:
            if doc.doc_id in delta:
                raise SnapshotCorruptionError(f"duplicate delta document: {doc.doc_id}")
            delta[doc.doc_id] = doc

        all_docs = [doc for segment in segments for doc in segment.documents] + delta_docs
        seen_versions: set[int] = set()
        latest: dict[str, _DocumentVersion] = {}
        for doc in all_docs:
            if doc.version in seen_versions:
                raise SnapshotCorruptionError(f"duplicate document version: {doc.version}")
            seen_versions.add(doc.version)
            if doc.version > version_clock:
                raise SnapshotCorruptionError("document version exceeds version clock")
            previous = latest.get(doc.doc_id)
            if previous is None or doc.version > previous.version:
                latest[doc.doc_id] = doc

        index._segments = segments
        index._delta = delta
        index._latest = latest
        index._generation = generation
        index._applied_offset = applied_offset
        index._version_clock = version_clock
        for doc in latest.values():
            index._add_live_stats_locked(doc)
        return index
