"""Reusable hybrid retrieval for natural-language knowledge bases.

``HybridKBRetriever`` indexes the same document text with the zero-dependency
Okapi BM25 implementation and, when enabled, a bge/faiss HNSW dense index.  At
query time it searches both routes concurrently, combines their rankings with
Reciprocal Rank Fusion (RRF), and can cross-encoder-rerank a bounded candidate
pool.

The heavy dependencies are optional.  Importing this module (and importing
``core.memory``) never imports faiss, torch, numpy, or sentence-transformers.
They are loaded only when a dense index or real reranker is constructed.  A
BM25-only instance therefore remains available in the zero-dependency core::

    retriever = HybridKBRetriever.from_corpus(corpus, fusion=False, rerank=False)
    hits = retriever.retrieve("administrator login lockout", k=5)

The default configuration is the full production pipeline.  Install the
``dense`` and ``rerank`` extras before constructing it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from core.memory.bm25 import BM25Index, tokenize
from core.memory.rrf import rrf_fuse

DEFAULT_DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class OptionalRetrievalDependencyError(ImportError):
    """Raised when an enabled retrieval stage is missing its optional extra."""


@dataclass(frozen=True)
class KBDocument:
    """One indexed knowledge-base document (normally a passage/chunk).

    ``metadata`` is returned untouched with a hit, so callers can retain source
    URLs, section ids, hierarchy, ACL information, or other corpus-specific
    fields without coupling the retriever to one schema.
    """

    id: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class _DenseIndex(Protocol):
    def search_texts(
        self,
        query_texts: Sequence[str],
        k: int,
        *,
        model_name: str | None = None,
    ) -> list[list[tuple[str, float]]]: ...


class _Reranker(Protocol):
    def rerank(
        self,
        query_text: str,
        candidates: Sequence[tuple[str, str]],
        top_k: int,
    ) -> list[str]: ...


class HybridKBRetriever:
    """BM25 + dense/HNSW + RRF + cross-encoder knowledge-base retriever.

    Parameters are deliberately stage-oriented so an online caller and an
    evaluation harness use the exact same component:

    - ``fusion=False`` selects BM25 alone and does not require dense packages.
    - ``fusion=True`` builds an HNSW index and fuses BM25+dense with RRF.
    - ``rerank=True`` cross-encoder-reranks the first-stage top-N candidates.
    - ``k`` and ``rerank_depth`` are defaults and can be overridden per call.

    ``dense_index`` and ``reranker_instance`` are dependency-injection seams for
    prebuilt/shared indexes, service adapters, and hermetic tests.  When omitted,
    the existing optional ``DenseIndex`` and ``CrossEncoderReranker`` are reused.
    """

    def __init__(
        self,
        documents: Sequence[KBDocument],
        *,
        fusion: bool = True,
        rerank: bool = True,
        k: int = 10,
        rerank_depth: int = 30,
        fusion_depth: int = 60,
        rrf_c: int = 60,
        model_name: str = DEFAULT_DENSE_MODEL,
        reranker_model: str = DEFAULT_RERANKER_MODEL,
        dense_cache_key: str | None = None,
        dense_index: _DenseIndex | None = None,
        reranker_instance: _Reranker | None = None,
    ) -> None:
        if k <= 0:
            raise ValueError("k must be positive")
        if rerank_depth <= 0:
            raise ValueError("rerank_depth must be positive")
        if fusion_depth <= 0:
            raise ValueError("fusion_depth must be positive")
        if rrf_c < 0:
            raise ValueError("rrf_c must be non-negative")
        if not documents:
            raise ValueError("documents must not be empty")

        by_id: dict[str, KBDocument] = {}
        for doc in documents:
            if not isinstance(doc, KBDocument):
                raise TypeError("documents must contain KBDocument instances")
            if not isinstance(doc.id, str) or not doc.id:
                raise ValueError("document ids must be non-empty strings")
            if not isinstance(doc.text, str):
                raise TypeError(f"document {doc.id!r} text must be a string")
            if not isinstance(doc.metadata, Mapping):
                raise TypeError(f"document {doc.id!r} metadata must be a mapping")
            if doc.id in by_id:
                raise ValueError(f"duplicate document id: {doc.id!r}")
            by_id[doc.id] = doc

        self.documents = by_id
        self.doc_ids = list(by_id)
        self.fusion = fusion
        self.rerank = rerank
        self.k = k
        self.rerank_depth = rerank_depth
        self.fusion_depth = fusion_depth
        self.rrf_c = rrf_c
        self.model_name = model_name
        self.reranker_model = reranker_model
        self.bm25 = BM25Index({doc_id: tokenize(doc.text) for doc_id, doc in by_id.items()})

        self.dense_index = dense_index
        if fusion and self.dense_index is None:
            self.dense_index = self._build_dense_index(dense_cache_key)

        self.reranker = reranker_instance
        if rerank and self.reranker is None:
            self.reranker = self._build_reranker()

    @classmethod
    def from_corpus(
        cls,
        corpus: Mapping[str, Any] | Iterable[KBDocument | Mapping[str, Any]],
        *,
        id_field: str = "id",
        text_field: str | None = None,
        contextual_headers: bool = True,
        **kwargs: Any,
    ) -> "HybridKBRetriever":
        """Construct from a common corpus representation.

        Accepted inputs:

        - ``{"doc-id": "document text", ...}``;
        - an iterable of :class:`KBDocument` or mapping records;
        - the FortiOS cache payload ``{"chunks": [records...], ...}``.

        For record corpora, ``text_field`` chooses the indexed field.  Otherwise
        ``cr_text`` is preferred, followed by ``context_header + text`` when
        ``contextual_headers`` is true, then ``text``.  All remaining record
        fields are retained as metadata.
        """
        records: Iterable[KBDocument | Mapping[str, Any]]
        if isinstance(corpus, Mapping) and "chunks" in corpus:
            chunks = corpus["chunks"]
            if not isinstance(chunks, Iterable) or isinstance(chunks, (str, bytes, Mapping)):
                raise TypeError("corpus['chunks'] must be an iterable of records")
            records = chunks
        elif isinstance(corpus, Mapping):
            records = [
                KBDocument(str(doc_id), text)
                if isinstance(text, str)
                else cls._document_from_record(
                    {id_field: doc_id, **dict(text)},
                    id_field=id_field,
                    text_field=text_field,
                    contextual_headers=contextual_headers,
                )
                for doc_id, text in corpus.items()
            ]
        else:
            records = corpus

        documents: list[KBDocument] = []
        for record in records:
            if isinstance(record, KBDocument):
                documents.append(record)
            elif isinstance(record, Mapping):
                documents.append(cls._document_from_record(
                    record,
                    id_field=id_field,
                    text_field=text_field,
                    contextual_headers=contextual_headers,
                ))
            else:
                raise TypeError("corpus records must be KBDocument or mapping instances")
        return cls(documents, **kwargs)

    @staticmethod
    def _document_from_record(
        record: Mapping[str, Any],
        *,
        id_field: str,
        text_field: str | None,
        contextual_headers: bool,
    ) -> KBDocument:
        if id_field not in record:
            raise ValueError(f"corpus record is missing id field {id_field!r}")
        doc_id = str(record[id_field])
        chosen_field = text_field
        if chosen_field is not None:
            if chosen_field not in record:
                raise ValueError(f"corpus record {doc_id!r} is missing text field {chosen_field!r}")
            text = record[chosen_field]
        elif isinstance(record.get("cr_text"), str):
            chosen_field = "cr_text"
            text = record[chosen_field]
        elif (
            contextual_headers
            and isinstance(record.get("context_header"), str)
            and isinstance(record.get("text"), str)
        ):
            text = f'{record["context_header"]}\n\n{record["text"]}'
        else:
            chosen_field = "text"
            text = record.get("text")
        if not isinstance(text, str):
            raise TypeError(f"corpus record {doc_id!r} does not contain string text")

        excluded = {id_field, "text", "cr_text"}
        metadata = {key: value for key, value in record.items() if key not in excluded}
        return KBDocument(id=doc_id, text=text, metadata=metadata)

    def _build_dense_index(self, cache_key: str | None) -> _DenseIndex:
        try:
            from core.eval.dense_retrieval import DenseIndex

            return DenseIndex.build(
                self.doc_ids,
                [self.documents[doc_id].text for doc_id in self.doc_ids],
                model_name=self.model_name,
                index_type="hnsw",
                cache_key=cache_key,
            )
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] in {
                "faiss", "numpy", "sentence_transformers", "torch", "transformers",
            }:
                raise OptionalRetrievalDependencyError(
                    "dense retrieval requires the optional dense extra; "
                    "install with `pip install -e '.[dense]'`"
                ) from exc
            raise

    def _build_reranker(self) -> _Reranker:
        # The wrapper itself is lightweight; sentence-transformers/torch load on
        # the first rerank call through the existing cached model loader.
        from core.eval.reranker import CrossEncoderReranker

        return CrossEncoderReranker(self.reranker_model)

    def retrieve(
        self,
        query: str,
        k: int | None = None,
        *,
        fusion: bool | None = None,
        rerank: bool | None = None,
        rerank_depth: int | None = None,
    ) -> list[KBDocument]:
        """Return the top documents for ``query`` in best-first order.

        Per-call flags make ablations and route degradation explicit.  Disabling
        fusion uses BM25 only.  Reranking may be applied to either the BM25 or
        hybrid pool.  Asking for a stage that was not constructed raises a clear
        error instead of silently changing the retrieval algorithm.
        """
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not query.strip():
            return []
        top_k = self.k if k is None else k
        if top_k <= 0:
            return []
        use_fusion = self.fusion if fusion is None else fusion
        use_rerank = self.rerank if rerank is None else rerank
        depth = self.rerank_depth if rerank_depth is None else rerank_depth
        if depth <= 0:
            raise ValueError("rerank_depth must be positive")

        rerank_pool = max(top_k, depth) if use_rerank else top_k
        route_depth = max(rerank_pool, self.fusion_depth) if use_fusion else rerank_pool

        if use_fusion:
            if self.dense_index is None:
                raise RuntimeError("fusion requested, but this retriever has no dense index")
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid-kb") as executor:
                bm25_future = executor.submit(self.bm25.rank, query, route_depth)
                dense_future = executor.submit(self.dense_index.search_texts, [query], route_depth,
                                               model_name=self.model_name)
                bm25_rank = bm25_future.result()
                dense_rows = dense_future.result()
            dense_rank = [doc_id for doc_id, _score in (dense_rows[0] if dense_rows else [])]
            first_stage = rrf_fuse([bm25_rank, dense_rank], rerank_pool, c=self.rrf_c)
        else:
            first_stage = self.bm25.rank(query, rerank_pool)

        ranking = first_stage
        if use_rerank:
            if self.reranker is None:
                raise RuntimeError("reranking requested, but this retriever has no reranker")
            pool = first_stage[:rerank_pool]
            candidates = [(doc_id, self.documents[doc_id].text) for doc_id in pool]
            try:
                reranked = self.reranker.rerank(query, candidates, rerank_pool)
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.split(".")[0] in {
                    "sentence_transformers", "torch", "transformers",
                }:
                    raise OptionalRetrievalDependencyError(
                        "reranking requires the optional rerank extra; "
                        "install with `pip install -e '.[rerank]'`"
                    ) from exc
                raise
            # Defend the component boundary against malformed adapters: ignore
            # duplicates/unknown ids and retain any omitted first-stage candidates.
            seen: set[str] = set()
            ranking = []
            for doc_id in [*reranked, *pool]:
                if doc_id in self.documents and doc_id not in seen:
                    seen.add(doc_id)
                    ranking.append(doc_id)

        return [self.documents[doc_id] for doc_id in ranking[:top_k]]

    def retrieve_ids(self, query: str, k: int | None = None, **kwargs: Any) -> list[str]:
        """Convenience form of :meth:`retrieve` that returns document ids only."""
        return [doc.id for doc in self.retrieve(query, k, **kwargs)]

    def __len__(self) -> int:
        return len(self.documents)


__all__ = [
    "DEFAULT_DENSE_MODEL",
    "DEFAULT_RERANKER_MODEL",
    "HybridKBRetriever",
    "KBDocument",
    "OptionalRetrievalDependencyError",
]
