"""Shared dense/embedding retrieval implementation and evaluation drivers — OPTIONAL.

This is the honest *dense* comparison baseline the rest of the project is measured
against. :class:`core.memory.hybrid_kb.HybridKBRetriever` lazily reuses the index
implementation for real knowledge-base retrieval; the comparison drivers remain here.
Everything is gated behind the optional ``dense`` extra (sentence-transformers +
faiss-cpu + torch), and merely importing the module does not load those packages.

Why it exists
-------------
The deterministic core retrieves with BM25 (sparse lexical) + a typed structured /
graph-path retriever + RRF fusion — no vectors anywhere. To state an HONEST
"structured/graph retrieval vs dense" number we need a REAL embedding retriever run
on the SAME real corpora, with the SAME non-leaking query/document text the lexical
retrievers see. That is what this module provides:

  * a sentence-transformers encoder (default ``BAAI/bge-small-en-v1.5`` — 384-dim,
    CPU-fast; the 8542-doc IODA pool embeds in a couple of minutes on CPU);
  * three faiss indexes over the embeddings — ``flat`` (exact inner-product /
    cosine), ``hnsw`` (approximate graph), and ``binary`` (sign-bit quantized,
    Hamming search) — so we can measure both retrieval quality AND the real
    full-precision-vs-binary memory ratio;
  * ``dense_retrieve(query, k)`` factories that match the existing retrievers'
    signature style so they drop into the harnesses as new baseline rows;
  * per-dataset comparison drivers that REUSE the existing eval harnesses' loaders,
    corpora and ground truth (``core.eval.ioda_retrieval``, ``skill_retrieval``,
    ``retrieval_precision``) and add the dense rows next to naive / bm25 /
    structured / rrf, reporting recall@k, precision@k and nDCG@k.

No-leakage: the dense query/document texts are rendered from exactly the same
operator-observable fields the lexical retrievers use (IODA: locations, ASNs,
outage type/cause, datasources, time is NOT given to dense; evidence doc text =
the same non-identifying ``_evidence_doc_text``). Dense sees no id, no label field.

HONESTY — the IODA "structured beats dense" number is a LABEL ARTIFACT, and the
report says so. The IODA relevance labels (``candidate_event_id``) were DEFINED by a
per-event entity+time-window pull, and the ``structured`` retriever scores documents by
that same entity+time key — it reconstructs the label-defining key, so its lead is
circular, not a retrieval win. ``run_ioda_dense_comparison`` therefore surfaces a FAIR,
text-only comparison BY DEFAULT (naive / bm25 / dense-* / structured_no_time / rrf-fair,
all with the time window withheld) and keeps ``structured`` / ``rrf`` / ``rrf+dense``
only as a clearly-labelled diagnostic upper bound. On the fair comparison dense does
NOT beat BM25 on this pool (the evidence docs carry almost no free-text signal) — that
is reported as measured, not hidden.
"""
from __future__ import annotations

import functools
import hashlib
import math
import os
import time
from pathlib import Path
from typing import Callable, Sequence

# Heavy deps (sentence-transformers / faiss / numpy) are imported lazily inside the
# functions that need them so that merely importing this module — e.g. to read a
# constant — does not require the optional extra to be installed.

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_HNSW_M = 32
DEFAULT_HNSW_EF_CONSTRUCTION = 200
DEFAULT_HNSW_EF_SEARCH = 128
# bge-* asymmetric retrieval: the short query gets an instruction prefix, passages do not.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

_CACHE_DIR = Path(os.environ.get("DENSE_CACHE_DIR") or (Path(__file__).resolve().parents[2] / ".dense_cache"))


# ── encoder ─────────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=2)
def _load_model(model_name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, device="cpu")


def _query_prefix(model_name: str) -> str:
    return BGE_QUERY_INSTRUCTION if "bge" in model_name.lower() else ""


def embed(
    texts: Sequence[str],
    *,
    model_name: str = DEFAULT_MODEL,
    is_query: bool = False,
    batch_size: int = 64,
    cache_key: str | None = None,
):
    """Return L2-normalized float32 embeddings ``(n, dim)`` for ``texts``.

    Normalized so that inner product == cosine similarity (faiss ``IndexFlatIP``).
    If ``cache_key`` is given, document embeddings are memoized on disk (keyed by
    model + key + a content hash) so repeated eval runs do not re-encode the pool.
    """
    import numpy as np

    prefix = _query_prefix(model_name) if is_query else ""
    payload = [prefix + t for t in texts]

    cache_path = None
    if cache_key is not None:
        h = hashlib.sha1(("\n".join(payload)).encode("utf-8")).hexdigest()[:16]
        safe_model = model_name.replace("/", "_")
        cache_path = _CACHE_DIR / f"{safe_model}__{cache_key}__{len(payload)}__{h}.npy"
        if cache_path.exists():
            return np.load(cache_path)

    model = _load_model(model_name)
    vecs = model.encode(
        payload,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    ).astype("float32")

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, vecs)
    return vecs


# ── faiss index wrapper ──────────────────────────────────────────────────────────
class DenseIndex:
    """A faiss index over a fixed document set, addressable by document id.

    ``index_type``:
      * ``"flat"``   — ``IndexFlatIP`` exact cosine search (the accuracy ceiling);
      * ``"hnsw"``   — ``IndexHNSWFlat`` approximate graph search (fast, ~exact here);
      * ``"binary"`` — sign-bit quantized ``IndexBinaryFlat`` Hamming search (the
                       memory-frugal baseline — 1 bit/dim instead of 32).

    Deterministic: score ties break on document id, matching the sparse retrievers.
    """

    def __init__(
        self,
        doc_ids: Sequence[str],
        embeddings,
        index_type: str = "flat",
        *,
        hnsw_m: int = DEFAULT_HNSW_M,
        hnsw_ef_construction: int = DEFAULT_HNSW_EF_CONSTRUCTION,
        hnsw_ef_search: int = DEFAULT_HNSW_EF_SEARCH,
    ):
        import faiss
        import numpy as np

        if len(doc_ids) != len(embeddings):
            raise ValueError("doc_ids and embeddings length mismatch")
        self.doc_ids = list(doc_ids)
        self.index_type = index_type
        self.dim = int(embeddings.shape[1])
        self._emb = np.ascontiguousarray(embeddings, dtype="float32")

        if index_type == "flat":
            self.index = faiss.IndexFlatIP(self.dim)
            self.index.add(self._emb)
        elif index_type == "hnsw":
            idx = faiss.IndexHNSWFlat(self.dim, hnsw_m, faiss.METRIC_INNER_PRODUCT)
            idx.hnsw.efConstruction = hnsw_ef_construction
            idx.hnsw.efSearch = hnsw_ef_search
            idx.add(self._emb)
            self.index = idx
        elif index_type == "binary":
            self._codes = np.packbits((self._emb > 0).astype("uint8"), axis=1)
            self.index = faiss.IndexBinaryFlat(self.dim)
            self.index.add(self._codes)
        else:
            raise ValueError(f"unknown index_type {index_type!r}")

    @classmethod
    def build(
        cls,
        doc_ids: Sequence[str],
        doc_texts: Sequence[str],
        *,
        model_name: str = DEFAULT_MODEL,
        index_type: str = "flat",
        cache_key: str | None = None,
        hnsw_m: int = DEFAULT_HNSW_M,
        hnsw_ef_construction: int = DEFAULT_HNSW_EF_CONSTRUCTION,
        hnsw_ef_search: int = DEFAULT_HNSW_EF_SEARCH,
    ) -> "DenseIndex":
        emb = embed(list(doc_texts), model_name=model_name, is_query=False, cache_key=cache_key)
        obj = cls(
            doc_ids,
            emb,
            index_type,
            hnsw_m=hnsw_m,
            hnsw_ef_construction=hnsw_ef_construction,
            hnsw_ef_search=hnsw_ef_search,
        )
        obj.model_name = model_name
        return obj

    def search_embeddings(self, query_emb, k: int) -> list[list[tuple[str, float]]]:
        import faiss
        import numpy as np

        if k <= 0:
            return [[] for _ in range(len(query_emb))]
        kk = min(k, len(self.doc_ids))
        if self.index_type == "binary":
            codes = np.packbits((np.ascontiguousarray(query_emb, dtype="float32") > 0).astype("uint8"), axis=1)
            dist, idx = self.index.search(codes, kk)
            # smaller Hamming distance == more similar -> similarity = -distance
            sims = -dist.astype("float32")
        else:
            q = np.ascontiguousarray(query_emb, dtype="float32")
            sims, idx = self.index.search(q, kk)
        out: list[list[tuple[str, float]]] = []
        for row_scores, row_idx in zip(sims, idx):
            pairs = [
                (self.doc_ids[i], float(s))
                for s, i in zip(row_scores, row_idx)
                if i >= 0
            ]
            # deterministic: sort by (-score, doc_id) so ties are stable across index types
            pairs.sort(key=lambda p: (-p[1], p[0]))
            out.append(pairs)
        return out

    def search_texts(self, query_texts: Sequence[str], k: int, *, model_name: str | None = None) -> list[list[tuple[str, float]]]:
        mn = model_name or getattr(self, "model_name", DEFAULT_MODEL)
        qemb = embed(list(query_texts), model_name=mn, is_query=True)
        return self.search_embeddings(qemb, k)

    # ── memory accounting ────────────────────────────────────────────────────────
    def vector_bytes(self) -> int:
        """Raw stored-vector footprint (the quantity the '内存降 Nx' claim is about)."""
        if self.index_type == "binary":
            return int(self._codes.nbytes)
        return int(len(self.doc_ids) * self.dim * 4)  # float32

    def serialized_bytes(self) -> int:
        """Real on-disk/serialized faiss index size (includes graph/struct overhead)."""
        import faiss

        if self.index_type == "binary":
            return int(len(faiss.serialize_index_binary(self.index)))
        return int(len(faiss.serialize_index(self.index)))


def make_dense_retriever(index: DenseIndex, *, model_name: str | None = None) -> Callable[[str, int], list[str]]:
    """A ``retrieve(query_text, k) -> [doc_id, ...]`` closure matching the sparse retrievers."""

    def retrieve(query: str, k: int) -> list[str]:
        if k <= 0:
            return []
        return [d for d, _ in index.search_texts([query], k, model_name=model_name)[0]]

    return retrieve


# ── metrics (binary relevance; shared by every dataset driver) ───────────────────
def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_ranking(retrieved: list[str], relevant: set[str], k: int) -> dict:
    top = retrieved[:k]
    hits = [r for r in top if r in relevant]
    n_rel = len(relevant)
    gains = [1.0 if r in relevant else 0.0 for r in top]
    idcg = _dcg([1.0] * min(k, n_rel)) if n_rel else 0.0
    ndcg = (_dcg(gains) / idcg) if idcg else 0.0
    return {
        "recall_at_k": len(hits) / n_rel if n_rel else 0.0,
        "precision_at_k": len(hits) / k if k else 0.0,
        "ndcg_at_k": ndcg,
        "false_retrieval": (len(top) - len(hits)) / max(1, len(top)),
    }


def _macro(rows: list[dict]) -> dict:
    if not rows:
        return {m: 0.0 for m in ("recall_at_k", "precision_at_k", "ndcg_at_k", "false_retrieval")}
    return {
        m: round(sum(r[m] for r in rows) / len(rows), 4)
        for m in ("recall_at_k", "precision_at_k", "ndcg_at_k", "false_retrieval")
    }


def _memory_report(idx_flat: DenseIndex, idx_binary: DenseIndex, idx_hnsw: DenseIndex | None = None) -> dict:
    """Full-precision vs binary-quantized footprint — the honest '内存降 Nx' number."""
    rep = {
        "n_docs": len(idx_flat.doc_ids),
        "dim": idx_flat.dim,
        "flat_vector_bytes": idx_flat.vector_bytes(),
        "binary_vector_bytes": idx_binary.vector_bytes(),
        "flat_index_bytes": idx_flat.serialized_bytes(),
        "binary_index_bytes": idx_binary.serialized_bytes(),
    }
    rep["vector_reduction_x"] = round(rep["flat_vector_bytes"] / rep["binary_vector_bytes"], 2)
    rep["index_reduction_x"] = round(rep["flat_index_bytes"] / rep["binary_index_bytes"], 2)
    if idx_hnsw is not None:
        rep["hnsw_index_bytes"] = idx_hnsw.serialized_bytes()
    return rep


# ══════════════════════════════════════════════════════════════════════════════════
# Dataset driver 1 — REAL IODA v2 three-source pool (8542 docs / 832 events)
# ══════════════════════════════════════════════════════════════════════════════════
def _ioda_query_text(event: dict) -> str:
    """Operator-observable query text (same non-leaking fields the lexical retriever uses).

    Rendered as light natural language (dense encoders expect prose) but strictly from
    locations / ASNs / outage type+cause / datasources — never an id, label, or the
    time window (time is the STRUCTURED retriever's lever, deliberately withheld here).
    """
    locations = [str(x) for x in (event.get("locations") or [])]
    asns = [str(x) for x in (event.get("asns") or [])]
    parts: list[str] = []
    otype = str(event.get("outage_type") or "").replace("_", " ").lower().strip()
    ocause = str(event.get("outage_cause") or "").replace("_", " ").lower().strip()
    if otype or ocause:
        parts.append(f"{otype} outage caused by {ocause}".strip())
    if locations:
        parts.append("in country " + " ".join(locations))
    if asns:
        parts.append("affecting AS " + " ".join(asns))
    ds = [str(d).replace("-", " ").replace("_", " ") for d in (event.get("ioda_v2_datasources") or [])]
    if ds:
        parts.append("observed via " + " ".join(ds))
    return " ".join(p for p in parts if p).strip() or (otype or "network outage")


# The FAIR comparison: every method sees ONLY operator-observable text/entities that
# carry no label information. ``structured_no_time`` is the typed entity retriever with
# the time axis removed, so it is a legitimate text/metadata baseline. ``rrf-fair`` fuses
# only fair routes (bm25 + structured_no_time + dense).
_IODA_FAIR_METHODS = ["naive", "bm25", "dense-flat", "dense-hnsw", "dense-binary",
                      "structured_no_time", "rrf-fair"]
# The LABEL-KEY UPPER BOUND: a DIAGNOSTIC, not a fair baseline. See ``_IODA_UPPER_BOUND_NOTE``.
_IODA_UPPER_BOUND_METHODS = ["structured", "rrf", "rrf+dense"]
_IODA_FAIR_NOTE = (
    "FAIR, text-only comparison (NO time window). Apples-to-apples: every method sees "
    "only operator-observable text/entities (country/ASN/outage words/source hints) that "
    "carry no label information. These are the honest retrieval numbers."
)
_IODA_UPPER_BOUND_NOTE = (
    "DIAGNOSTIC — NOT a fair retrieval baseline. The relevance labels "
    "(candidate_event_id) were DEFINED by a per-event entity+time-window pull, and "
    "'structured' scores documents by that exact same entity+time key: it reconstructs "
    "the label-defining key. 'rrf'/'rrf+dense' fuse 'structured' in and inherit the leak, "
    "and the time window is deliberately withheld from every fair method. Read these as an "
    "UPPER BOUND on what the join key can recover — never as a retrieval win over dense/BM25."
)


def run_ioda_dense_comparison(
    *,
    max_events: int | None = None,
    model_name: str = DEFAULT_MODEL,
    k_values: tuple[int, ...] = (1, 5, 10),
    include_binary: bool = True,
    include_hnsw: bool = True,
    path=None,
) -> dict:
    """Compare dense vs sparse retrieval on the real IODA v2 pool — FAIR by default.

    Reuses ``core.eval.ioda_retrieval`` end-to-end: same corpus, same non-leaking doc
    text, same typed retrievers, same ground truth. Dense sees only the operator-
    observable query text (no time window).

    The DEFAULT surfaced result is the FAIR, text-only comparison (``fair_methods``):
    naive / bm25 / dense-{flat,hnsw,binary} / structured_no_time / rrf-fair — every one
    of which sees only label-free text/entities. The time-using retrievers
    (``upper_bound_methods``: structured / rrf / rrf+dense) are KEPT but relabelled as a
    diagnostic upper bound, because the relevance labels are DEFINED by the same
    entity+time key the structured retriever scores on (see ``_IODA_UPPER_BOUND_NOTE``):
    a circular, label-reconstructing number, not a fair baseline.
    """
    from core.memory.rrf import rrf_fuse
    from core.eval import ioda_retrieval as R

    evidence = R.load_evidence(path)
    doc_ids = [R._doc_id(rec["evidence_id"]) for rec in evidence]
    doc_texts = [R._evidence_doc_text(rec) for rec in evidence]

    events = R.load_events(path)
    if max_events is not None:
        events = events[:max_events]
    query_texts = [_ioda_query_text(e) for e in events]
    event_ids = [e["event_id"] for e in events]

    relevant = R._relevant_sets(path)
    sparse = R.build_retrievers("base", path)              # naive / bm25 / structured / structured_no_time / rrf
    queries = R.build_queries("base", path, max_events)    # aligned with events

    k_max = max(k_values)

    # dense indexes (doc embeddings cached on disk keyed by pool identity)
    cache_key = f"ioda_v2_{len(doc_ids)}"
    t0 = time.time()
    idx_flat = DenseIndex.build(doc_ids, doc_texts, model_name=model_name, index_type="flat", cache_key=cache_key)
    embed_secs = round(time.time() - t0, 1)
    idx_hnsw = DenseIndex.build(doc_ids, doc_texts, model_name=model_name, index_type="hnsw", cache_key=cache_key) if include_hnsw else None
    idx_binary = DenseIndex.build(doc_ids, doc_texts, model_name=model_name, index_type="binary", cache_key=cache_key) if include_binary else None

    # batch dense retrieval (one encode of all queries per index)
    dense_flat_res = idx_flat.search_texts(query_texts, k_max, model_name=model_name)
    dense_hnsw_res = idx_hnsw.search_texts(query_texts, k_max, model_name=model_name) if idx_hnsw else None
    dense_bin_res = idx_binary.search_texts(query_texts, k_max, model_name=model_name) if idx_binary else None

    # split fair vs upper-bound, honouring include_* toggles
    fair_methods = [m for m in _IODA_FAIR_METHODS
                    if not (m == "dense-hnsw" and not include_hnsw)
                    and not (m == "dense-binary" and not include_binary)]
    methods = fair_methods + _IODA_UPPER_BOUND_METHODS

    acc: dict[str, dict[int, list[dict]]] = {m: {k: [] for k in k_values} for m in methods}

    for i, (q, ev_id) in enumerate(zip(queries, event_ids)):
        rel = relevant.get(ev_id, set())
        ranked: dict[str, list[str]] = {
            # ── fair (text-only, no time) ──
            "naive": sparse["naive"](q, k_max),
            "bm25": sparse["bm25"](q, k_max),
            "structured_no_time": sparse["structured_no_time"](q, k_max),
            "dense-flat": [d for d, _ in dense_flat_res[i]],
            # ── label-key upper bound (uses the time window that DEFINES the labels) ──
            "structured": sparse["structured"](q, k_max),
            "rrf": sparse["rrf"](q, k_max),
        }
        if dense_hnsw_res is not None:
            ranked["dense-hnsw"] = [d for d, _ in dense_hnsw_res[i]]
        if dense_bin_res is not None:
            ranked["dense-binary"] = [d for d, _ in dense_bin_res[i]]
        # fair fusion: RRF over fair routes only (bm25 + structured_no_time + dense-flat).
        ranked["rrf-fair"] = rrf_fuse(
            [ranked["bm25"], ranked["structured_no_time"], ranked["dense-flat"]], k_max
        )
        # upper-bound fusion: adds a dense route to the time-leaking structured retriever.
        ranked["rrf+dense"] = rrf_fuse([ranked["bm25"], ranked["structured"], ranked["dense-flat"]], k_max)
        for m in methods:
            for k in k_values:
                acc[m][k].append(score_ranking(ranked[m], rel, k))

    hk = 10 if 10 in k_values else k_max
    fair_headline = {m: round(sum(r["recall_at_k"] for r in acc[m][hk]) / len(acc[m][hk]), 3)
                     for m in fair_methods} if acc[fair_methods[0]][hk] else {}
    out: dict = {
        "dataset_kind": "real-ioda-v2-three-source",
        "model": model_name,
        "n_queries": len(events),
        "n_corpus_docs": len(doc_ids),
        "k_values": list(k_values),
        "embed_seconds_flat": embed_secs,
        "fair_note": _IODA_FAIR_NOTE,
        "fair_methods": fair_methods,
        "upper_bound_note": _IODA_UPPER_BOUND_NOTE,
        "upper_bound_methods": list(_IODA_UPPER_BOUND_METHODS),
        "fair_headline_recall_at_{}".format(hk): fair_headline,
        "methods": {m: {k: _macro(acc[m][k]) for k in k_values} for m in methods},
    }
    if include_binary:
        out["memory"] = _memory_report(idx_flat, idx_binary, idx_hnsw)
    return out


# ══════════════════════════════════════════════════════════════════════════════════
# Dataset driver 2 — REAL FortiGate held-out skill catalog (9 skills / 6 queries)
# ══════════════════════════════════════════════════════════════════════════════════
def run_skill_dense_comparison(
    *,
    model_name: str = DEFAULT_MODEL,
    k_values: tuple[int, ...] = (1, 2, 3),
    path=None,
) -> dict:
    """Add a dense-flat row to the real FortiGate held-out skill-retrieval eval."""
    from core.memory.bm25 import tokenize  # noqa: F401  (kept for parity of vocabulary intent)
    from core.memory.rrf import rrf_fuse
    from core.eval import skill_retrieval as S
    from domains.network_rca.skills.real_skills import REAL_SKILL_OPERATIONS

    doc_ids: list[str] = []
    doc_texts: list[str] = []
    for name, (operation, tag_list) in REAL_SKILL_OPERATIONS.items():
        description = f"Readonly real-syslog RCA check for {operation}"
        text = " ".join([name.replace("_", " "), operation.replace("_", " "), description, " ".join(tag_list)])
        doc_ids.append(name)
        doc_texts.append(text)

    cases = S.load_heldout(path)
    sparse = S.build_retrievers("base")
    idx_flat = DenseIndex.build(doc_ids, doc_texts, model_name=model_name, index_type="flat")
    dense = make_dense_retriever(idx_flat, model_name=model_name)

    methods = ["naive", "bm25", "dense-flat", "structured", "rrf", "rrf+dense"]
    acc = {m: {k: [] for k in k_values} for m in methods}
    for c in cases:
        rel = c["relevant"]
        for k in k_values:
            pool = max(k, max(k_values))
            ranked = {
                "naive": sparse["naive"](c["query"], k),
                "bm25": sparse["bm25"](c["query"], k),
                "structured": sparse["structured"](c["query"], k),
                "rrf": sparse["rrf"](c["query"], k),
                "dense-flat": dense(c["query"], k),
            }
            ranked["rrf+dense"] = rrf_fuse(
                [sparse["bm25"](c["query"], pool), sparse["structured"](c["query"], pool), dense(c["query"], pool)], k
            )
            for m in methods:
                acc[m][k].append(score_ranking(ranked[m], rel, k))

    return {
        "dataset_kind": "real-fortigate-heldout",
        "model": model_name,
        "n_queries": len(cases),
        "n_corpus_docs": len(doc_ids),
        "k_values": list(k_values),
        "methods": {m: {k: _macro(acc[m][k]) for k in k_values} for m in methods},
    }


# ══════════════════════════════════════════════════════════════════════════════════
# Dataset driver 3 — synthetic topology fixture (logical/graph vs dense)
# ══════════════════════════════════════════════════════════════════════════════════
def _topo_query_text(query: dict) -> str:
    return " ".join([
        " ".join(str(e) for e in query.get("entities", [])),
        str(query.get("relation") or ""),
        str(query.get("intent") or ""),
    ]).strip()


def run_topo_dense_comparison(*, model_name: str = DEFAULT_MODEL, fixtures=None) -> dict:
    """Add a dense-flat row to the synthetic topology retrieval-precision eval."""
    from core.eval import retrieval_precision as P
    from core.memory.logical_retrieval import (
        _flatten_record,
        logical_retrieve,
        naive_similarity_retrieve,
    )
    from core.memory.topo_graph import TopoGraphMemory

    fixture = P.load_fixture(fixtures)
    records = fixture.get("records", [])
    queries = fixture.get("queries", [])
    graph = TopoGraphMemory(records)

    recs = list(graph.all_records())
    doc_ids = [r.id for r in recs]
    doc_texts = [_flatten_record(r) for r in recs]
    idx_flat = DenseIndex.build(doc_ids, doc_texts, model_name=model_name, index_type="flat")

    methods = ["logical", "naive", "dense-flat"]
    acc = {m: [] for m in methods}
    per_query: dict[str, dict] = {}
    for item in queries:
        q = item["query"]
        k = int(item.get("k", 3))
        rel = set(item.get("relevant_ids", []))
        ranked = {
            "logical": [r.id for r in logical_retrieve(q, graph, k)],
            "naive": [r.id for r in naive_similarity_retrieve(q, records, k)],
            "dense-flat": [d for d, _ in idx_flat.search_texts([_topo_query_text(q)], k, model_name=model_name)[0]],
        }
        for m in methods:
            acc[m].append(score_ranking(ranked[m], rel, k))
        per_query[item["id"]] = {m: ranked[m] for m in methods} | {"relevant": sorted(rel)}

    return {
        "dataset_kind": fixture.get("dataset_kind", "synthetic-topology-eval"),
        "model": model_name,
        "n_queries": len(queries),
        "n_corpus_docs": len(doc_ids),
        "methods": {m: _macro(acc[m]) for m in methods},
        "per_query": per_query,
    }


# ── CLI reporting ────────────────────────────────────────────────────────────────
def _fmt_row(name: str, by_k: dict, ks: list[int], metric: str) -> str:
    return name.ljust(14) + "".join(f"{by_k[k][metric]:.3f}".rjust(9) for k in ks)


def _wrap(text: str, width: int = 76, indent: str = "  ") -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent))


def _print_ioda_group(res: dict, method_names: list[str], ks: list[int]) -> None:
    for metric, label in (("recall_at_k", "recall@k"), ("ndcg_at_k", "nDCG@k"), ("precision_at_k", "precision@k")):
        print(f"\n{label}:")
        print("method".ljust(20) + "".join(f"@{k}".rjust(9) for k in ks))
        print("-" * (20 + 9 * len(ks)))
        for m in method_names:
            print(m.ljust(20) + "".join(f"{res['methods'][m][k][metric]:.3f}".rjust(9) for k in ks))


def _print_ioda(res: dict) -> None:
    ks = res["k_values"]
    fair = res.get("fair_methods") or list(res["methods"])
    upper = res.get("upper_bound_methods") or []
    print("=" * 78)
    print(f"IODA v2 three-source pool — {res['n_corpus_docs']} docs / {res['n_queries']} events "
          f"(model={res['model']}, embed {res['embed_seconds_flat']}s)")

    print("\n" + "#" * 78)
    print("# FAIR COMPARISON (text-only, NO time window) — the honest retrieval numbers")
    print("#" * 78)
    if "fair_note" in res:
        print(_wrap(res["fair_note"]))
    _print_ioda_group(res, fair, ks)
    hk = 10 if 10 in ks else ks[-1]
    hkey = f"fair_headline_recall_at_{hk}"
    if res.get(hkey):
        order = sorted(res[hkey].items(), key=lambda kv: kv[1])
        print(f"\nfair headline (recall@{hk}, worst→best): "
              + "  ".join(f"{m} {v:.3f}" for m, v in order))

    if upper:
        print("\n" + "#" * 78)
        print("# LABEL-KEY UPPER BOUND (diagnostic — NOT a fair baseline)")
        print("#" * 78)
        if "upper_bound_note" in res:
            print(_wrap(res["upper_bound_note"]))
        _print_ioda_group(res, upper, ks)

    if "memory" in res:
        mem = res["memory"]
        print(f"\nindex memory ({mem['n_docs']} docs x {mem['dim']}-dim):")
        print(f"  flat float32 vectors : {mem['flat_vector_bytes']/1024:.1f} KiB  "
              f"(faiss index {mem['flat_index_bytes']/1024:.1f} KiB)")
        print(f"  binary (sign-bit)    : {mem['binary_vector_bytes']/1024:.1f} KiB  "
              f"(faiss index {mem['binary_index_bytes']/1024:.1f} KiB)")
        print(f"  reduction            : vectors {mem['vector_reduction_x']}x, "
              f"serialized index {mem['index_reduction_x']}x")


def _print_generic(res: dict, methods_order: list[str]) -> None:
    print("=" * 78)
    print(f"{res['dataset_kind']} — {res['n_corpus_docs']} docs / {res['n_queries']} queries (model={res['model']})")
    if "k_values" in res:
        ks = res["k_values"]
        for metric, label in (("recall_at_k", "recall@k"), ("ndcg_at_k", "nDCG@k"), ("precision_at_k", "precision@k")):
            print(f"\n{label}:")
            print("method".ljust(14) + "".join(f"@{k}".rjust(9) for k in ks))
            print("-" * (14 + 9 * len(ks)))
            for m in methods_order:
                print(_fmt_row(m, res["methods"][m], ks, metric))
    else:  # topo: single-k aggregate
        print("\nmethod        precision@k  recall@k   nDCG@k  false-retr")
        print("-" * 58)
        for m in methods_order:
            r = res["methods"][m]
            print(f"{m.ljust(14)}{r['precision_at_k']:>10.3f}{r['recall_at_k']:>10.3f}"
                  f"{r['ndcg_at_k']:>9.3f}{r['false_retrieval']:>11.3f}")


def main(argv: list[str] | None = None) -> int:
    import json as _json

    which = (argv or ["all"])[0]
    if which in ("all", "ioda"):
        res = run_ioda_dense_comparison()
        _print_ioda(res)
        (_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / "ioda_dense_result.json").write_text(_json.dumps(res, indent=2))
    if which in ("all", "skill"):
        res = run_skill_dense_comparison()
        _print_generic(res, ["naive", "bm25", "dense-flat", "structured", "rrf", "rrf+dense"])
    if which in ("all", "topo"):
        res = run_topo_dense_comparison()
        _print_generic(res, ["logical", "naive", "dense-flat"])
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
