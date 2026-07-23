"""Text-facing adapter for the mutable FAISS memory-vector lifecycle.

``VectorIndexLifecycle`` deliberately owns vectors rather than text.  This
module is the thin online boundary that embeds memory text and queries, while
preserving the lifecycle's immutable Flat-by-default base, exact Flat delta, version table,
tombstones and atomic compaction semantics.

Heavy retrieval dependencies stay optional.  Importing this module does not
import NumPy, FAISS, torch or sentence-transformers.  They are loaded only when
an embedding or vector-index operation is actually requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from core.memory.vector_lifecycle import SearchHit, VectorIndexLifecycle


_OPTIONAL_DEPENDENCIES = {
    "faiss",
    "numpy",
    "sentence_transformers",
    "torch",
    "transformers",
}


class VectorMemoryDependencyError(ImportError):
    """A vector-memory operation needs an optional dependency that is absent."""


@runtime_checkable
class TextEmbedder(Protocol):
    """Embedding boundary used by the online memory index.

    Implementations must use the same vector space for documents and queries.
    Asymmetric retrieval models may still apply different query instructions in
    ``embed_queries``.  The protocol intentionally returns ``Any`` so importing
    it never makes NumPy a mandatory dependency.
    """

    dimension: int
    model_id: str

    def embed_documents(self, texts: Sequence[str]) -> Any: ...

    def embed_queries(self, texts: Sequence[str]) -> Any: ...


@dataclass(frozen=True)
class BGETextEmbedder:
    """Production adapter around the project's sentence-transformer encoder.

    The default model is the already evaluated 384-dimensional BGE encoder.
    Supplying a different model requires its real output dimension; a mismatch
    is rejected by :class:`VectorIndexLifecycle` instead of silently corrupting
    the index.  This class contains no synthetic/hash embedding fallback.
    """

    model_id: str = "BAAI/bge-small-en-v1.5"
    dimension: int = 384
    batch_size: int = 64

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id must be non-empty")
        if self.dimension <= 0:
            raise ValueError("dimension must be positive")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")

    def embed_documents(self, texts: Sequence[str]) -> Any:
        from core.eval.dense_retrieval import embed

        return embed(
            texts,
            model_name=self.model_id,
            is_query=False,
            batch_size=self.batch_size,
        )

    def embed_queries(self, texts: Sequence[str]) -> Any:
        from core.eval.dense_retrieval import embed

        return embed(
            texts,
            model_name=self.model_id,
            is_query=True,
            batch_size=self.batch_size,
        )


@dataclass(frozen=True)
class VectorMemoryHit:
    """One live memory candidate returned from the merged base/delta view."""

    memory_id: str
    score: float
    version: int


def _validate_embedder(embedder: TextEmbedder) -> None:
    if not isinstance(getattr(embedder, "dimension", None), int):
        raise TypeError("embedder.dimension must be an integer")
    if embedder.dimension <= 0:
        raise ValueError("embedder.dimension must be positive")
    if not isinstance(getattr(embedder, "model_id", None), str) or not embedder.model_id:
        raise ValueError("embedder.model_id must be a non-empty string")
    if not callable(getattr(embedder, "embed_documents", None)):
        raise TypeError("embedder must implement embed_documents(texts)")
    if not callable(getattr(embedder, "embed_queries", None)):
        raise TypeError("embedder must implement embed_queries(texts)")


def _translate_optional_dependency(exc: ModuleNotFoundError) -> None:
    missing = (exc.name or "").split(".")[0]
    if missing in _OPTIONAL_DEPENDENCIES:
        raise VectorMemoryDependencyError(
            "vector memory requires optional dense dependencies; install with "
            "`pip install -e '.[dense]'`"
        ) from exc
    raise exc


def _first_embedding(batch: Any, *, operation: str) -> Any:
    """Extract one row without importing or assuming a NumPy return type."""
    try:
        if len(batch) != 1:
            raise ValueError(f"embedder must return one vector for {operation}")
        return batch[0]
    except TypeError as exc:
        raise TypeError("embedder output must be a sized sequence or matrix") from exc


class VectorMemoryIndex:
    """Online semantic memory index over an immutable base and exact mutable delta.

    The adapter contains no authoritative memory text or metadata.  PostgreSQL
    or :class:`TieredMemoryStore` remains the source of truth; this object is a
    rebuildable retrieval projection keyed by string ``memory_id``.
    """

    def __init__(self, embedder: TextEmbedder, lifecycle: VectorIndexLifecycle) -> None:
        _validate_embedder(embedder)
        if lifecycle.dimension != embedder.dimension:
            raise ValueError(
                f"index dimension {lifecycle.dimension} does not match embedder "
                f"dimension {embedder.dimension}"
            )
        self.embedder = embedder
        self.lifecycle = lifecycle

    @classmethod
    def build(
        cls,
        documents: Mapping[str, str],
        embedder: TextEmbedder,
        *,
        applied_offset: int = 0,
        **lifecycle_options: Any,
    ) -> "VectorMemoryIndex":
        """Embed a corpus snapshot and build the first immutable base generation."""
        _validate_embedder(embedder)
        if not isinstance(documents, Mapping):
            raise TypeError("documents must be a mapping of memory_id to text")
        ids = list(documents)
        for memory_id, text in documents.items():
            if not isinstance(memory_id, str) or not memory_id:
                raise ValueError("memory ids must be non-empty strings")
            if not isinstance(text, str):
                raise TypeError(f"memory {memory_id!r} text must be a string")
        try:
            if ids:
                embeddings = embedder.embed_documents([documents[memory_id] for memory_id in ids])
                lifecycle = VectorIndexLifecycle.build(
                    ids,
                    embeddings,
                    applied_offset=applied_offset,
                    **lifecycle_options,
                )
            else:
                lifecycle = VectorIndexLifecycle(embedder.dimension, **lifecycle_options)
                if isinstance(applied_offset, bool) or not isinstance(applied_offset, int):
                    raise TypeError("applied_offset must be an integer")
                if applied_offset < 0:
                    raise ValueError("applied_offset must be non-negative")
                lifecycle.applied_offset = applied_offset
        except ModuleNotFoundError as exc:
            _translate_optional_dependency(exc)
        if lifecycle.dimension != embedder.dimension:
            raise ValueError(
                f"embedding dimension {lifecycle.dimension} does not match declared "
                f"embedder dimension {embedder.dimension}"
            )
        return cls(embedder, lifecycle)

    def upsert(
        self,
        memory_id: str,
        text: str,
        *,
        offset: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Encode and append a new memory version to the exact Flat delta."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        try:
            vector = _first_embedding(
                self.embedder.embed_documents([text]), operation="upsert"
            )
            return self.lifecycle.upsert(memory_id, vector, offset=offset, version=version)
        except ModuleNotFoundError as exc:
            _translate_optional_dependency(exc)

    def delete(
        self,
        memory_id: str,
        *,
        offset: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Immediately hide one memory through lifecycle version/tombstone state."""
        return self.lifecycle.delete(memory_id, offset=offset, version=version)

    def search(self, query: str, k: int = 10) -> list[VectorMemoryHit]:
        """Encode a query and return scored, de-duplicated live memory versions."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if k <= 0:
            return []
        try:
            vector = _first_embedding(
                self.embedder.embed_queries([query]), operation="search"
            )
            return [self._hit(hit) for hit in self.lifecycle.search_hits(vector, k)]
        except ModuleNotFoundError as exc:
            _translate_optional_dependency(exc)

    def compact(self) -> int:
        """Rebuild the configured base from live versions and atomically install it."""
        try:
            return self.lifecycle.compact()
        except ModuleNotFoundError as exc:
            _translate_optional_dependency(exc)

    def should_compact(self) -> bool:
        return self.lifecycle.should_compact()

    def health(self) -> dict[str, int | float | bool | str]:
        """Return serving lifecycle metrics plus embedding identity."""
        return {
            **self.lifecycle.health(),
            "dimension": self.lifecycle.dimension,
            "embedding_model": self.embedder.model_id,
            "base_index_type": self.lifecycle.base_index_type,
        }

    @staticmethod
    def _hit(hit: SearchHit) -> VectorMemoryHit:
        return VectorMemoryHit(hit.doc_id, hit.score, hit.version)
