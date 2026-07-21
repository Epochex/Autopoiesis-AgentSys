"""Crash-safe lifecycle for a mutable corpus backed by immutable FAISS indexes.

FAISS HNSW is treated as an immutable serving snapshot.  New document versions
are appended to a small exact (``IndexFlatIP``) delta.  A version table makes
superseded and deleted vectors invisible immediately; compaction rebuilds a new
HNSW from only the live versions and swaps it in after the build succeeds.

The class deliberately accepts embeddings rather than text.  Embedding/model
versioning belongs to the caller, while this module owns vector visibility,
generation management and durable snapshots.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


FORMAT_VERSION = 1


class IndexSnapshotError(RuntimeError):
    """The durable index snapshot is incomplete, corrupt or inconsistent."""


class ConcurrentCompactionError(RuntimeError):
    """The corpus changed while a replacement base index was being built."""


@dataclass(frozen=True)
class DocumentVersion:
    version: int
    deleted: bool
    offset: int


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    score: float
    version: int


@dataclass(frozen=True)
class IndexStats:
    generation: int
    applied_offset: int
    live_documents: int
    base_vectors: int
    delta_vectors: int
    obsolete_vectors: int
    tombstones: int


@dataclass(frozen=True)
class _Entry:
    doc_id: str
    version: int


def _numpy():
    import numpy as np

    return np


def _dense_index_class():
    from core.eval.dense_retrieval import DenseIndex

    return DenseIndex


def _normalise_vector(vector: Any, dimension: int):
    np = _numpy()
    value = np.asarray(vector, dtype="float32")
    if value.ndim != 1 or value.shape[0] != dimension:
        raise ValueError(f"embedding must have shape ({dimension},)")
    if not np.isfinite(value).all():
        raise ValueError("embedding must contain only finite values")
    norm = float(np.linalg.norm(value))
    if norm == 0.0:
        raise ValueError("embedding must not be the zero vector")
    return np.ascontiguousarray(value / norm, dtype="float32")


def _normalise_matrix(embeddings: Any, dimension: int | None = None):
    np = _numpy()
    value = np.asarray(embeddings, dtype="float32")
    if value.ndim != 2:
        raise ValueError("embeddings must be a two-dimensional matrix")
    if dimension is not None and value.shape[1] != dimension:
        raise ValueError(f"embeddings must have dimension {dimension}")
    if not np.isfinite(value).all():
        raise ValueError("embeddings must contain only finite values")
    norms = np.linalg.norm(value, axis=1)
    if (norms == 0.0).any():
        raise ValueError("embeddings must not contain zero vectors")
    return np.ascontiguousarray(value / norms[:, None], dtype="float32")


def _empty_matrix(dimension: int):
    return _numpy().empty((0, dimension), dtype="float32")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class VectorIndexLifecycle:
    """Mutable document view over an immutable base and an exact delta index.

    ``offset`` is a monotonically increasing source-event position.  Operations
    at or below ``applied_offset`` are idempotently ignored, which makes replay
    after a process restart safe.  ``version`` defaults to the next version for
    that document and may be supplied when the source owns version assignment.
    """

    def __init__(
        self,
        dimension: int,
        *,
        base_index_type: str = "hnsw",
        delta_max_entries: int = 10_000,
        delta_ratio_threshold: float = 0.10,
        obsolete_ratio_threshold: float = 0.15,
        hnsw_m: int = 32,
        hnsw_ef_construction: int = 200,
        hnsw_ef_search: int = 128,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        if base_index_type not in {"hnsw", "flat"}:
            raise ValueError("base_index_type must be 'hnsw' or 'flat'")
        if delta_max_entries <= 0:
            raise ValueError("delta_max_entries must be positive")
        if min(hnsw_m, hnsw_ef_construction, hnsw_ef_search) <= 0:
            raise ValueError("HNSW parameters must be positive")
        for name, value in {
            "delta_ratio_threshold": delta_ratio_threshold,
            "obsolete_ratio_threshold": obsolete_ratio_threshold,
        }.items():
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")

        self.dimension = dimension
        self.base_index_type = base_index_type
        self.delta_max_entries = delta_max_entries
        self.delta_ratio_threshold = delta_ratio_threshold
        self.obsolete_ratio_threshold = obsolete_ratio_threshold
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.hnsw_ef_search = hnsw_ef_search

        self.generation = 0
        self.applied_offset = 0
        self._versions: dict[str, DocumentVersion] = {}
        self._base_entries: list[_Entry] = []
        self._base_vectors = _empty_matrix(dimension)
        self._base_index = None
        self._delta_entries: list[_Entry] = []
        self._delta_vectors: list[Any] = []
        self._delta_index = None
        self._delta_dirty = False
        self._mutation_serial = 0
        self._lock = threading.RLock()

    @classmethod
    def build(
        cls,
        doc_ids: Sequence[str],
        embeddings: Any,
        *,
        applied_offset: int = 0,
        **kwargs: Any,
    ) -> "VectorIndexLifecycle":
        """Build generation one directly from a corpus snapshot."""
        if len(set(doc_ids)) != len(doc_ids):
            raise ValueError("doc_ids must be unique")
        if any(not isinstance(doc_id, str) or not doc_id for doc_id in doc_ids):
            raise ValueError("doc_ids must be non-empty strings")
        matrix = _normalise_matrix(embeddings)
        if len(doc_ids) != len(matrix):
            raise ValueError("doc_ids and embeddings length mismatch")
        if matrix.shape[1] <= 0:
            raise ValueError("embedding dimension must be positive")
        if isinstance(applied_offset, bool) or not isinstance(applied_offset, int):
            raise TypeError("applied_offset must be an integer")
        if applied_offset < 0:
            raise ValueError("applied_offset must be non-negative")

        result = cls(int(matrix.shape[1]), **kwargs)
        result.applied_offset = applied_offset
        result.generation = 1
        result._base_entries = [_Entry(doc_id, 1) for doc_id in doc_ids]
        result._base_vectors = matrix
        result._versions = {
            doc_id: DocumentVersion(version=1, deleted=False, offset=applied_offset)
            for doc_id in doc_ids
        }
        result._base_index = result._make_index(result._base_entries, matrix, result.base_index_type)
        return result

    @property
    def versions(self) -> dict[str, DocumentVersion]:
        """Return a copy so callers cannot bypass lifecycle invariants."""
        with self._lock:
            return dict(self._versions)

    @property
    def stats(self) -> IndexStats:
        with self._lock:
            physical = len(self._base_entries) + len(self._delta_entries)
            live = sum(not item.deleted for item in self._versions.values())
            return IndexStats(
                generation=self.generation,
                applied_offset=self.applied_offset,
                live_documents=live,
                base_vectors=len(self._base_entries),
                delta_vectors=len(self._delta_entries),
                obsolete_vectors=max(0, physical - live),
                tombstones=sum(item.deleted for item in self._versions.values()),
            )

    def upsert(
        self,
        doc_id: str,
        embedding: Any,
        *,
        offset: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Append a document version to the exact delta.

        Returns ``False`` for an already-applied event offset and ``True`` when
        the mutation was accepted.
        """
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        vector = _normalise_vector(embedding, self.dimension)
        with self._lock:
            resolved_offset = self._resolve_offset(offset)
            if resolved_offset is None:
                return False
            current = self._versions.get(doc_id)
            resolved_version = self._resolve_version(current, version)
            self._delta_entries.append(_Entry(doc_id, resolved_version))
            self._delta_vectors.append(vector)
            self._versions[doc_id] = DocumentVersion(resolved_version, False, resolved_offset)
            self.applied_offset = resolved_offset
            self._delta_dirty = True
            self._mutation_serial += 1
            return True

    def delete(
        self,
        doc_id: str,
        *,
        offset: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Make a document invisible without attempting HNSW in-place deletion."""
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        with self._lock:
            resolved_offset = self._resolve_offset(offset)
            if resolved_offset is None:
                return False
            current = self._versions.get(doc_id)
            resolved_version = self._resolve_version(current, version)
            self._versions[doc_id] = DocumentVersion(resolved_version, True, resolved_offset)
            self.applied_offset = resolved_offset
            self._mutation_serial += 1
            return True

    def search(self, query_embedding: Any, k: int = 10) -> list[str]:
        """Return best-first document ids from the merged live view."""
        return [hit.doc_id for hit in self.search_hits(query_embedding, k)]

    def search_hits(self, query_embedding: Any, k: int = 10) -> list[SearchHit]:
        """Return scored hits after stale-version filtering and route merging."""
        if k <= 0:
            return []
        query = _normalise_vector(query_embedding, self.dimension).reshape(1, -1)
        with self._lock:
            self._ensure_delta_index()
            candidates: dict[str, SearchHit] = {}
            self._collect_candidates(
                self._base_index,
                self._base_entries,
                query,
                k,
                candidates,
            )
            self._collect_candidates(
                self._delta_index,
                self._delta_entries,
                query,
                k,
                candidates,
            )
            return sorted(candidates.values(), key=lambda hit: (-hit.score, hit.doc_id))[:k]

    def health(self) -> dict[str, int | float | bool]:
        """Expose the small set of lifecycle metrics needed by serving alarms."""
        stats = self.stats
        physical = stats.base_vectors + stats.delta_vectors
        return {
            "healthy": True,
            "generation": stats.generation,
            "applied_offset": stats.applied_offset,
            "physical_vectors": physical,
            "live": stats.live_documents,
            "tombstones": stats.tombstones,
            "base": stats.base_vectors,
            "delta": stats.delta_vectors,
            "obsolete": stats.obsolete_vectors,
            "obsolete_ratio": stats.obsolete_vectors / physical if physical else 0.0,
            "compaction_due": self.should_compact(),
        }

    def should_compact(self) -> bool:
        """Return whether delta growth or stale-vector density crossed a limit."""
        with self._lock:
            delta = len(self._delta_entries)
            physical = len(self._base_entries) + delta
            live = sum(not value.deleted for value in self._versions.values())
            obsolete = max(0, physical - live)
            if delta >= self.delta_max_entries:
                return True
            if self._base_entries and delta / len(self._base_entries) >= self.delta_ratio_threshold:
                return True
            return bool(physical and obsolete / physical >= self.obsolete_ratio_threshold)

    def compact(self) -> int:
        """Build a fresh base from live versions and atomically swap in memory.

        The expensive FAISS build runs without the mutation lock.  If a writer
        changes the corpus meanwhile, the replacement is discarded rather than
        losing that write.  A scheduler can retry the compaction later.
        """
        with self._lock:
            serial = self._mutation_serial
            source_generation = self.generation
            live_rows: list[tuple[_Entry, Any]] = []
            latest_vectors: dict[tuple[str, int], Any] = {}
            for entry, vector in zip(self._base_entries, self._base_vectors):
                latest_vectors[(entry.doc_id, entry.version)] = vector
            for entry, vector in zip(self._delta_entries, self._delta_vectors):
                latest_vectors[(entry.doc_id, entry.version)] = vector
            for doc_id, state in self._versions.items():
                if state.deleted:
                    continue
                vector = latest_vectors.get((doc_id, state.version))
                if vector is None:
                    raise IndexSnapshotError(f"live vector is missing for document {doc_id!r}")
                live_rows.append((_Entry(doc_id, state.version), vector))
            live_rows.sort(key=lambda row: row[0].doc_id)
            entries = [row[0] for row in live_rows]
            matrix = (
                _numpy().ascontiguousarray([row[1] for row in live_rows], dtype="float32")
                if live_rows
                else _empty_matrix(self.dimension)
            )

        replacement = self._make_index(entries, matrix, self.base_index_type)

        with self._lock:
            if serial != self._mutation_serial or source_generation != self.generation:
                raise ConcurrentCompactionError("corpus changed during compaction")
            self._base_entries = entries
            self._base_vectors = matrix
            self._base_index = replacement
            self._delta_entries = []
            self._delta_vectors = []
            self._delta_index = None
            self._delta_dirty = False
            # The source event stream owns replay and audit history. Once the old
            # physical generation is gone, retaining deleted ids in every future
            # index snapshot would simply move bloat into the version metadata.
            self._versions = {
                doc_id: state for doc_id, state in self._versions.items() if not state.deleted
            }
            self.generation += 1
            return self.generation

    def save(self, directory: str | os.PathLike[str], *, keep_snapshots: int = 2) -> Path:
        """Persist a checksummed snapshot and atomically advance ``CURRENT``."""
        if keep_snapshots <= 0:
            raise ValueError("keep_snapshots must be positive")
        import faiss

        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_delta_index()
            snapshot_name = f"snapshot-{self.generation:08d}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=root))
            final = root / snapshot_name
            try:
                metadata = self._metadata()
                metadata_path = temporary / "metadata.json"
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                np = _numpy()
                np.save(temporary / "base_vectors.npy", self._base_vectors, allow_pickle=False)
                delta_matrix = (
                    np.ascontiguousarray(self._delta_vectors, dtype="float32")
                    if self._delta_vectors
                    else _empty_matrix(self.dimension)
                )
                np.save(temporary / "delta_vectors.npy", delta_matrix, allow_pickle=False)
                if self._base_index is not None:
                    faiss.write_index(self._base_index.index, str(temporary / "base.faiss"))

                payload_files = sorted(path.name for path in temporary.iterdir())
                file_manifest = {
                    name: {"bytes": (temporary / name).stat().st_size, "sha256": _sha256(temporary / name)}
                    for name in payload_files
                }
                manifest = {
                    "format_version": FORMAT_VERSION,
                    "snapshot": snapshot_name,
                    "generation": self.generation,
                    "applied_offset": self.applied_offset,
                    "files": file_manifest,
                }
                manifest_path = temporary / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                for path in temporary.iterdir():
                    _fsync_file(path)
                _fsync_directory(temporary)
                os.replace(temporary, final)
                _fsync_directory(root)

                descriptor, pointer_tmp = tempfile.mkstemp(prefix=".CURRENT-", dir=root, text=True)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        handle.write(snapshot_name + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(pointer_tmp, root / "CURRENT")
                finally:
                    if os.path.exists(pointer_tmp):
                        os.unlink(pointer_tmp)
                _fsync_directory(root)
                self._remove_old_snapshots(root, keep_snapshots, snapshot_name)
                return final
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    @classmethod
    def load(cls, directory: str | os.PathLike[str]) -> "VectorIndexLifecycle":
        """Load and verify the snapshot named by the atomic ``CURRENT`` pointer."""
        import faiss

        root = Path(directory)
        pointer = root / "CURRENT"
        try:
            snapshot_name = pointer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise IndexSnapshotError("CURRENT pointer is missing or unreadable") from exc
        if not snapshot_name or Path(snapshot_name).name != snapshot_name:
            raise IndexSnapshotError("CURRENT contains an invalid snapshot name")
        snapshot = root / snapshot_name
        manifest_path = snapshot / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IndexSnapshotError("manifest is missing or invalid") from exc
        cls._validate_manifest(snapshot, snapshot_name, manifest)

        try:
            metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
            if metadata.get("format_version") != FORMAT_VERSION:
                raise IndexSnapshotError("unsupported metadata format")
            dimension = int(metadata["dimension"])
            result = cls(
                dimension,
                base_index_type=metadata["base_index_type"],
                delta_max_entries=int(metadata["delta_max_entries"]),
                delta_ratio_threshold=float(metadata["delta_ratio_threshold"]),
                obsolete_ratio_threshold=float(metadata["obsolete_ratio_threshold"]),
                hnsw_m=int(metadata["hnsw_m"]),
                hnsw_ef_construction=int(metadata["hnsw_ef_construction"]),
                hnsw_ef_search=int(metadata["hnsw_ef_search"]),
            )
            result.generation = int(metadata["generation"])
            result.applied_offset = int(metadata["applied_offset"])
            if result.generation != int(manifest["generation"]):
                raise IndexSnapshotError("generation differs between manifest and metadata")
            if result.applied_offset != int(manifest["applied_offset"]):
                raise IndexSnapshotError("applied_offset differs between manifest and metadata")
            result._versions = {
                item["doc_id"]: DocumentVersion(
                    version=int(item["version"]),
                    deleted=bool(item["deleted"]),
                    offset=int(item["offset"]),
                )
                for item in metadata["versions"]
            }
            result._base_entries = [_Entry(str(item["doc_id"]), int(item["version"])) for item in metadata["base_entries"]]
            result._delta_entries = [_Entry(str(item["doc_id"]), int(item["version"])) for item in metadata["delta_entries"]]
            np = _numpy()
            result._base_vectors = np.load(snapshot / "base_vectors.npy", allow_pickle=False)
            delta_matrix = np.load(snapshot / "delta_vectors.npy", allow_pickle=False)
            result._delta_vectors = [row.copy() for row in delta_matrix]
            result._validate_loaded_state()
            if result._base_entries:
                raw_index = faiss.read_index(str(snapshot / "base.faiss"))
                result._base_index = result._wrap_loaded_index(
                    result._base_entries,
                    result._base_vectors,
                    result.base_index_type,
                    raw_index,
                )
            result._delta_dirty = bool(result._delta_entries)
            result._ensure_delta_index()
            return result
        except IndexSnapshotError:
            raise
        except Exception as exc:
            raise IndexSnapshotError("snapshot contents are inconsistent") from exc

    def _resolve_offset(self, offset: int | None) -> int | None:
        resolved = self.applied_offset + 1 if offset is None else offset
        if isinstance(resolved, bool) or not isinstance(resolved, int):
            raise TypeError("offset must be an integer")
        if resolved < 0:
            raise ValueError("offset must be non-negative")
        if resolved <= self.applied_offset:
            return None
        return resolved

    @staticmethod
    def _resolve_version(current: DocumentVersion | None, version: int | None) -> int:
        minimum = 1 if current is None else current.version + 1
        resolved = minimum if version is None else version
        if isinstance(resolved, bool) or not isinstance(resolved, int):
            raise TypeError("version must be an integer")
        if resolved < minimum:
            raise ValueError(f"version must be at least {minimum}")
        return resolved

    def _make_index(self, entries: Sequence[_Entry], vectors: Any, index_type: str):
        if not entries:
            return None
        DenseIndex = _dense_index_class()
        tokens = [str(position) for position in range(len(entries))]
        return DenseIndex(
            tokens,
            vectors,
            index_type=index_type,
            hnsw_m=self.hnsw_m,
            hnsw_ef_construction=self.hnsw_ef_construction,
            hnsw_ef_search=self.hnsw_ef_search,
        )

    def _ensure_delta_index(self) -> None:
        if not self._delta_dirty:
            return
        matrix = (
            _numpy().ascontiguousarray(self._delta_vectors, dtype="float32")
            if self._delta_vectors
            else _empty_matrix(self.dimension)
        )
        self._delta_index = self._make_index(self._delta_entries, matrix, "flat")
        self._delta_dirty = False

    def _collect_candidates(
        self,
        index: Any,
        entries: Sequence[_Entry],
        query: Any,
        k: int,
        output: dict[str, SearchHit],
    ) -> None:
        if index is None:
            return
        # Do not request k + every tombstone up front: at 20% churn that turns a
        # top-10 lookup into a 20,010-neighbour search.  Expand geometrically only
        # when the nearest window is unusually stale.
        depth = min(len(entries), max(k, k * 2))
        route_hits: dict[str, SearchHit] = {}
        while depth:
            for token, score in index.search_embeddings(query, depth)[0]:
                entry = entries[int(token)]
                if not self._is_current(entry):
                    continue
                hit = SearchHit(entry.doc_id, score, entry.version)
                previous = route_hits.get(entry.doc_id)
                if previous is None or hit.score > previous.score:
                    route_hits[entry.doc_id] = hit
            if len(route_hits) >= k or depth == len(entries):
                break
            depth = min(len(entries), depth * 2)
        for doc_id, hit in route_hits.items():
            previous = output.get(doc_id)
            if previous is None or hit.score > previous.score:
                output[doc_id] = hit

    def _is_current(self, entry: _Entry) -> bool:
        state = self._versions.get(entry.doc_id)
        return state is not None and not state.deleted and state.version == entry.version

    def _metadata(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "dimension": self.dimension,
            "base_index_type": self.base_index_type,
            "delta_max_entries": self.delta_max_entries,
            "delta_ratio_threshold": self.delta_ratio_threshold,
            "obsolete_ratio_threshold": self.obsolete_ratio_threshold,
            "hnsw_m": self.hnsw_m,
            "hnsw_ef_construction": self.hnsw_ef_construction,
            "hnsw_ef_search": self.hnsw_ef_search,
            "generation": self.generation,
            "applied_offset": self.applied_offset,
            "versions": [
                {"doc_id": doc_id, **asdict(value)}
                for doc_id, value in sorted(self._versions.items())
            ],
            "base_entries": [asdict(entry) for entry in self._base_entries],
            "delta_entries": [asdict(entry) for entry in self._delta_entries],
        }

    @classmethod
    def _validate_manifest(cls, snapshot: Path, snapshot_name: str, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise IndexSnapshotError("unsupported manifest format")
        if manifest.get("snapshot") != snapshot_name:
            raise IndexSnapshotError("manifest snapshot name does not match CURRENT")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise IndexSnapshotError("manifest contains no payload files")
        for name, expected in files.items():
            if Path(name).name != name or not isinstance(expected, dict):
                raise IndexSnapshotError("manifest contains an invalid file entry")
            path = snapshot / name
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise IndexSnapshotError(f"snapshot file {name!r} is missing") from exc
            if size != expected.get("bytes") or _sha256(path) != expected.get("sha256"):
                raise IndexSnapshotError(f"snapshot file {name!r} failed checksum validation")
        required = {"metadata.json", "base_vectors.npy", "delta_vectors.npy"}
        if not required.issubset(files):
            raise IndexSnapshotError("manifest is missing required payload files")

    def _validate_loaded_state(self) -> None:
        if self._base_vectors.shape != (len(self._base_entries), self.dimension):
            raise IndexSnapshotError("base vector matrix shape does not match metadata")
        if self._delta_entries and _numpy().asarray(self._delta_vectors).shape != (
            len(self._delta_entries),
            self.dimension,
        ):
            raise IndexSnapshotError("delta vector matrix shape does not match metadata")
        if not self._delta_entries and self._delta_vectors:
            raise IndexSnapshotError("delta vectors exist without metadata entries")
        all_entries = [*self._base_entries, *self._delta_entries]
        entry_keys = {(entry.doc_id, entry.version) for entry in all_entries}
        if len(entry_keys) != len(all_entries):
            raise IndexSnapshotError("snapshot contains duplicate vector versions")
        for entry in all_entries:
            state = self._versions.get(entry.doc_id)
            if state is None or entry.version > state.version:
                raise IndexSnapshotError("index entry is inconsistent with version table")
        for doc_id, state in self._versions.items():
            if state.version <= 0 or state.offset < 0 or state.offset > self.applied_offset:
                raise IndexSnapshotError("version table contains an invalid version or offset")
            if not state.deleted and (doc_id, state.version) not in entry_keys:
                raise IndexSnapshotError(f"live vector is missing for document {doc_id!r}")

    def _wrap_loaded_index(self, entries: Sequence[_Entry], vectors: Any, index_type: str, raw_index: Any):
        if int(raw_index.d) != self.dimension or int(raw_index.ntotal) != len(entries):
            raise IndexSnapshotError("FAISS index shape does not match metadata")
        DenseIndex = _dense_index_class()
        wrapped = DenseIndex.__new__(DenseIndex)
        wrapped.doc_ids = [str(position) for position in range(len(entries))]
        wrapped.index_type = index_type
        wrapped.dim = self.dimension
        wrapped._emb = vectors
        wrapped.index = raw_index
        return wrapped

    @staticmethod
    def _remove_old_snapshots(root: Path, keep: int, current: str) -> None:
        snapshots = sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name.startswith("snapshot-")),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        retained = {path.name for path in snapshots[:keep]}
        retained.add(current)
        for path in snapshots:
            if path.name not in retained:
                shutil.rmtree(path, ignore_errors=True)
