"""Shared cross-encoder reranker and two-stage evaluation drivers — OPTIONAL.

A two-stage retrieval eval: a cheap first stage (BM25 / dense / RRF) fetches a
candidate pool, then a cross-encoder rescores every (query, document) *pair jointly*
and re-orders the pool. Cross-encoders are the standard reranking model — unlike a
bi-encoder they read the query and document together, so they can catch relevance
that lexical/embedding first stages miss — at the cost of one forward pass per
candidate (why they only ever run over a top-k pool, never the whole corpus).

The reusable :class:`core.memory.hybrid_kb.HybridKBRetriever` lazily constructs
``CrossEncoderReranker`` for its optional final stage; the benchmark drivers remain
in this module.  The model runtime is gated behind the ``rerank`` extra
(``sentence-transformers`` + torch) and loads only on the first rerank call.

Three settings are measured, first-stage vs +reranker, on recall@k and nDCG@k:

  (a) the FAIR IODA eval (:func:`run_ioda_rerank_comparison`) — the same non-leaking,
      text-only IODA setup as the fair dense comparison. Expectation, stated up front
      and reported honestly: little/no lift, because the evidence documents carry
      almost no free-text signal for a language model to exploit.
  (b) the FortiGate skill-routing eval (:func:`run_skill_rerank_comparison`) — 6 real
      natural-language incident queries -> 9 read-only probe skills. This one has real
      NL semantics, so a cross-encoder has something to work with.
  (c) a SMALL PUBLIC IR BENCHMARK with real relevance judgments
      (:func:`run_scifact_rerank_comparison`) — BEIR SciFact (5183 docs / 300 test
      queries / binary qrels), downloaded from the public UKP mirror with the stdlib
      (no ``beir``/``ir_datasets``/``datasets`` package needed). This gives an
      externally-valid, non-circular "reranking lifts recall@10 by +X%" number that
      does NOT depend on this repo's data.

Every number is measured, never projected. Where reranking does not help, that is
reported as-is.
"""
from __future__ import annotations

import functools
import io
import os
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Sequence

from core.eval.dense_retrieval import _macro, score_ranking

DEFAULT_RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_BEIR_BASE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"
_CACHE_DIR = Path(os.environ.get("DENSE_CACHE_DIR") or (Path(__file__).resolve().parents[2] / ".dense_cache"))


# ── cross-encoder reranker ────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=2)
def _load_cross_encoder(model_name: str, max_length: int = 512):
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name, max_length=max_length, device="cpu")


class CrossEncoderReranker:
    """Rescore a candidate pool with a cross-encoder and return the re-ordered ids.

    ``rerank(query, candidates, top_k)`` scores each ``(query, doc_text)`` pair with a
    single joint forward pass, sorts by descending score, and returns the top-``top_k``
    document ids. Deterministic: score ties break on document id (matching the sparse /
    dense retrievers), so the ordering is reproducible across runs.
    """

    def __init__(self, model_name: str = DEFAULT_RERANKER, *, max_length: int = 512, batch_size: int = 64):
        self.model_name = model_name
        self.max_length = max_length
        self.batch_size = batch_size

    def rerank(self, query_text: str, candidates: Sequence[tuple[str, str]], top_k: int) -> list[str]:
        if top_k <= 0 or not candidates:
            return []
        model = _load_cross_encoder(self.model_name, self.max_length)
        pairs = [(query_text, doc_text) for _, doc_text in candidates]
        scores = model.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)
        scored = [(cid, float(s)) for (cid, _), s in zip(candidates, scores)]
        scored.sort(key=lambda p: (-p[1], p[0]))
        return [cid for cid, _ in scored[:top_k]]


# ── generic two-stage driver ───────────────────────────────────────────────────────
def _eval_first_stage_then_rerank(
    *,
    queries: Sequence[tuple[str, set[str]]],   # (query_text, relevant_ids)
    first_stage: Callable[[str, int], list[str]],
    doc_text_of: Callable[[str], str],
    reranker: CrossEncoderReranker,
    rerank_depth: int,
    k_values: tuple[int, ...],
) -> dict:
    """Run the first stage, rerank its top-``rerank_depth``, and macro-average both.

    Returns ``{"first_stage": {k: metrics}, "reranked": {k: metrics}, ...}``.
    The reranker only re-orders the pool the first stage returned, so its recall@k is
    capped by the first stage's recall@``rerank_depth`` (reported as ``pool_recall``).
    """
    k_max = max(k_values)
    depth = max(rerank_depth, k_max)
    fs_acc = {k: [] for k in k_values}
    rr_acc = {k: [] for k in k_values}
    pool_hit = 0
    pool_rel = 0
    t_fs = t_rr = 0.0
    for qtext, rel in queries:
        t0 = time.time()
        pool = first_stage(qtext, depth)
        t_fs += time.time() - t0
        # pool recall ceiling (how many relevant the first stage even surfaced)
        pool_hit += len(set(pool) & rel)
        pool_rel += len(rel)
        cands = [(cid, doc_text_of(cid)) for cid in pool]
        t0 = time.time()
        reranked = reranker.rerank(qtext, cands, depth)
        t_rr += time.time() - t0
        for k in k_values:
            fs_acc[k].append(score_ranking(pool, rel, k))
            rr_acc[k].append(score_ranking(reranked, rel, k))
    return {
        "first_stage": {k: _macro(fs_acc[k]) for k in k_values},
        "reranked": {k: _macro(rr_acc[k]) for k in k_values},
        "pool_recall": round(pool_hit / pool_rel, 4) if pool_rel else 0.0,
        "rerank_depth": depth,
        "first_stage_seconds": round(t_fs, 2),
        "rerank_seconds": round(t_rr, 2),
    }


def _delta_table(res: dict, k_values: tuple[int, ...]) -> dict:
    """Recall@k / nDCG@k lift of reranking over the first stage, absolute and relative."""
    out = {}
    for metric in ("recall_at_k", "ndcg_at_k"):
        for k in k_values:
            fs = res["first_stage"][k][metric]
            rr = res["reranked"][k][metric]
            out[f"{metric}@{k}"] = {
                "first_stage": fs,
                "reranked": rr,
                "abs_delta": round(rr - fs, 4),
                "rel_delta_pct": round((rr - fs) / fs * 100, 1) if fs else None,
            }
    return out


# ══════════════════════════════════════════════════════════════════════════════════
# Setting (a) — FAIR IODA eval (text-only; expect little/no lift, reported honestly)
# ══════════════════════════════════════════════════════════════════════════════════
def run_ioda_rerank_comparison(
    *,
    model_name: str = DEFAULT_RERANKER,
    first_stage_name: str = "bm25",
    rerank_depth: int = 50,
    k_values: tuple[int, ...] = (1, 5, 10),
    max_events: int | None = None,
    path=None,
) -> dict:
    """BM25 (fair, text-only) first stage + cross-encoder rerank on the IODA v2 pool.

    Same non-leaking setup as the fair dense comparison: the query is the operator-
    observable text (:func:`core.eval.dense_retrieval._ioda_query_text`), the document
    is the non-identifying ``_evidence_doc_text`` — no time window, no id, no label.
    """
    from core.eval import dense_retrieval as D
    from core.eval import ioda_retrieval as R

    evidence = R.load_evidence(path)
    doc_text = {R._doc_id(rec["evidence_id"]): R._evidence_doc_text(rec) for rec in evidence}
    relevant = R._relevant_sets(path)

    sparse = R.build_retrievers("base", path)
    lex_queries = R.build_queries("base", path, max_events)   # aligned with events (lexical Query objs)
    events = R.load_events(path)
    if max_events is not None:
        events = events[:max_events]

    # Only FAIR (no-time) first stages are offered here; 'rrf'/'structured' fuse the
    # time window that DEFINES the labels and would leak, so they are intentionally absent.
    fs_fn_map = {
        "bm25": lambda q, k: sparse["bm25"](q, k),
        "structured_no_time": lambda q, k: sparse["structured_no_time"](q, k),
    }
    # Build (query_text, relevant, lexical_query) triples; the cross-encoder sees the
    # prose query text while the first stage consumes the lexical Query object.
    triples = []
    for ev, lexq in zip(events, lex_queries):
        triples.append((D._ioda_query_text(ev), relevant.get(ev["event_id"], set()), lexq))

    reranker = CrossEncoderReranker(model_name)
    k_max = max(k_values)
    depth = max(rerank_depth, k_max)
    fs_acc = {k: [] for k in k_values}
    rr_acc = {k: [] for k in k_values}
    pool_hit = pool_rel = 0
    t_fs = t_rr = 0.0
    fs_retrieve = fs_fn_map[first_stage_name]
    for qtext, rel, lexq in triples:
        t0 = time.time()
        pool = fs_retrieve(lexq, depth)
        t_fs += time.time() - t0
        pool_hit += len(set(pool) & rel)
        pool_rel += len(rel)
        cands = [(cid, doc_text[cid]) for cid in pool]
        t0 = time.time()
        reranked = reranker.rerank(qtext, cands, depth)
        t_rr += time.time() - t0
        for k in k_values:
            fs_acc[k].append(score_ranking(pool, rel, k))
            rr_acc[k].append(score_ranking(reranked, rel, k))
    res = {
        "first_stage": {k: _macro(fs_acc[k]) for k in k_values},
        "reranked": {k: _macro(rr_acc[k]) for k in k_values},
        "pool_recall": round(pool_hit / pool_rel, 4) if pool_rel else 0.0,
        "rerank_depth": depth,
        "first_stage_seconds": round(t_fs, 2),
        "rerank_seconds": round(t_rr, 2),
    }
    return {
        "setting": "ioda-fair",
        "dataset_kind": "real-ioda-v2-three-source",
        "note": ("FAIR IODA: text-only, no time window. Expectation stated up front: the "
                 "evidence documents carry almost no free-text signal, so a cross-encoder "
                 "has little to exploit — little/no lift expected, reported as measured."),
        "reranker_model": model_name,
        "first_stage": first_stage_name,
        "n_queries": len(triples),
        "n_corpus_docs": len(doc_text),
        "k_values": list(k_values),
        "results": res,
        "delta": _delta_table(res, k_values),
    }


# ══════════════════════════════════════════════════════════════════════════════════
# Setting (b) — FortiGate skill-routing eval (real NL queries -> skills)
# ══════════════════════════════════════════════════════════════════════════════════
def run_skill_rerank_comparison(
    *,
    model_name: str = DEFAULT_RERANKER,
    first_stage_name: str = "bm25",
    rerank_depth: int = 9,
    k_values: tuple[int, ...] = (1, 2, 3),
    path=None,
) -> dict:
    """BM25 (or RRF) first stage + cross-encoder rerank on the FortiGate skill catalog.

    Query = the real held-out case's natural-language incident text. Document = the
    skill's name + operation + description + curated tags (same text the lexical
    retrievers see). This setting has genuine NL semantics, so a reranker can help.
    """
    from core.eval import skill_retrieval as S
    from domains.network_rca.skills.real_skills import REAL_SKILL_OPERATIONS

    doc_text: dict[str, str] = {}
    for name, (operation, tag_list) in REAL_SKILL_OPERATIONS.items():
        description = f"Readonly real-syslog RCA check for {operation}"
        doc_text[name] = " ".join(
            [name.replace("_", " "), operation.replace("_", " "), description, " ".join(tag_list)]
        )

    cases = S.load_heldout(path)
    sparse = S.build_retrievers("base")
    first_stage = (lambda q, k: sparse[first_stage_name](q, k))
    queries = [(c["query"], c["relevant"]) for c in cases]

    reranker = CrossEncoderReranker(model_name)
    res = _eval_first_stage_then_rerank(
        queries=queries,
        first_stage=first_stage,
        doc_text_of=lambda cid: doc_text[cid],
        reranker=reranker,
        rerank_depth=min(rerank_depth, len(doc_text)),
        k_values=k_values,
    )
    return {
        "setting": "fortigate-skill-routing",
        "dataset_kind": "real-fortigate-heldout",
        "note": "6 real NL incident queries -> 9 read-only skills; real NL semantics.",
        "reranker_model": model_name,
        "first_stage": first_stage_name,
        "n_queries": len(cases),
        "n_corpus_docs": len(doc_text),
        "k_values": list(k_values),
        "results": res,
        "delta": _delta_table(res, k_values),
    }


# ══════════════════════════════════════════════════════════════════════════════════
# Setting (c) — BEIR SciFact (public IR benchmark, real relevance judgments)
# ══════════════════════════════════════════════════════════════════════════════════
def _download_beir(dataset: str = "scifact") -> Path:
    """Download+extract a BEIR dataset zip from the public UKP mirror (stdlib only).

    Returns the extracted dataset directory (``<cache>/beir/<dataset>``). Raises a
    clear ``RuntimeError`` if the download fails so the caller can fall back to (a)(b).
    """
    dest_root = _CACHE_DIR / "beir"
    dest = dest_root / dataset
    if (dest / "corpus.jsonl").exists() and (dest / "queries.jsonl").exists():
        return dest
    url = f"{_BEIR_BASE}/{dataset}.zip"
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not download BEIR {dataset} from {url}: {e!r}") from e
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest_root)
    if not (dest / "corpus.jsonl").exists():
        raise RuntimeError(f"BEIR {dataset} zip extracted but corpus.jsonl missing at {dest}")
    return dest


def load_beir(dataset: str = "scifact", split: str = "test") -> dict:
    """Load a BEIR dataset into ``{corpus, queries, qrels}`` (pure stdlib parsing).

    ``corpus``  : ``{doc_id: "title text"}``
    ``queries`` : ``{query_id: query_text}`` restricted to those judged in ``split``
    ``qrels``   : ``{query_id: {doc_id, ...}}`` (positive judgments, score > 0)
    """
    import json

    root = _download_beir(dataset)
    qrels: dict[str, set[str]] = {}
    with (root / "qrels" / f"{split}.tsv").open(encoding="utf-8") as f:
        header = next(f)  # query-id\tcorpus-id\tscore
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            qid, cid, score = parts[0], parts[1], int(parts[2])
            if score > 0:
                qrels.setdefault(qid, set()).add(cid)
    corpus: dict[str, str] = {}
    with (root / "corpus.jsonl").open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            corpus[rec["_id"]] = (str(rec.get("title") or "") + " " + str(rec.get("text") or "")).strip()
    queries: dict[str, str] = {}
    with (root / "queries.jsonl").open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["_id"] in qrels:
                queries[rec["_id"]] = rec["text"]
    return {"corpus": corpus, "queries": queries, "qrels": qrels, "dataset": dataset, "split": split}


def run_scifact_rerank_comparison(
    *,
    model_name: str = DEFAULT_RERANKER,
    dataset: str = "scifact",
    rerank_depth: int = 100,
    k_values: tuple[int, ...] = (1, 5, 10),
    max_queries: int | None = None,
) -> dict:
    """BM25 first stage + cross-encoder rerank on BEIR SciFact — real qrels, non-circular.

    First stage is a pure-Python Okapi BM25 (:mod:`core.memory.bm25`) over the whole
    corpus; the cross-encoder reranks its top-``rerank_depth``. This is the externally-
    valid reranking number: the relevance judgments are the public BEIR qrels, defined
    by human annotators with no relationship to this repo's retrievers.
    """
    from core.memory.bm25 import BM25Index, tokenize

    data = load_beir(dataset, "test")
    corpus, queries, qrels = data["corpus"], data["queries"], data["qrels"]

    doc_tokens = {cid: tokenize(text) for cid, text in corpus.items()}
    bm25 = BM25Index(doc_tokens)

    qids = sorted(queries)
    if max_queries is not None:
        qids = qids[:max_queries]

    reranker = CrossEncoderReranker(model_name)
    k_max = max(k_values)
    depth = max(rerank_depth, k_max)
    fs_acc = {k: [] for k in k_values}
    rr_acc = {k: [] for k in k_values}
    pool_hit = pool_rel = 0
    t_fs = t_rr = 0.0
    for qid in qids:
        qtext = queries[qid]
        rel = qrels[qid]
        t0 = time.time()
        pool = [cid for cid, _ in bm25.rank_with_scores(qtext, depth)]
        t_fs += time.time() - t0
        pool_hit += len(set(pool) & rel)
        pool_rel += len(rel)
        cands = [(cid, corpus[cid]) for cid in pool]
        t0 = time.time()
        reranked = reranker.rerank(qtext, cands, depth)
        t_rr += time.time() - t0
        for k in k_values:
            fs_acc[k].append(score_ranking(pool, rel, k))
            rr_acc[k].append(score_ranking(reranked, rel, k))
    res = {
        "first_stage": {k: _macro(fs_acc[k]) for k in k_values},
        "reranked": {k: _macro(rr_acc[k]) for k in k_values},
        "pool_recall": round(pool_hit / pool_rel, 4) if pool_rel else 0.0,
        "rerank_depth": depth,
        "first_stage_seconds": round(t_fs, 2),
        "rerank_seconds": round(t_rr, 2),
    }
    return {
        "setting": "beir-scifact",
        "dataset_kind": f"beir-{dataset}-{data['split']}",
        "note": ("Public BEIR benchmark, real human relevance judgments — an externally-valid, "
                 "non-circular reranking number that does not depend on this repo's data."),
        "reranker_model": model_name,
        "first_stage": "bm25",
        "n_queries": len(qids),
        "n_corpus_docs": len(corpus),
        "k_values": list(k_values),
        "results": res,
        "delta": _delta_table(res, k_values),
    }


# ── reporting ───────────────────────────────────────────────────────────────────
def _print_rerank(res: dict) -> None:
    ks = res["k_values"]
    r = res["results"]
    print("=" * 78)
    print(f"[{res['setting']}] {res['dataset_kind']} — {res['n_corpus_docs']} docs / "
          f"{res['n_queries']} queries")
    print(f"  first stage : {res['first_stage']}   reranker: {res['reranker_model']}")
    print(f"  note        : {res['note']}")
    print(f"  pool recall@{r['rerank_depth']} (first-stage ceiling): {r['pool_recall']:.3f}   "
          f"(first-stage {r['first_stage_seconds']}s, rerank {r['rerank_seconds']}s)")
    for metric, label in (("recall_at_k", "recall@k"), ("ndcg_at_k", "nDCG@k")):
        print(f"\n  {label}:")
        print("    stage".ljust(20) + "".join(f"@{k}".rjust(9) for k in ks))
        print("    " + "-" * (16 + 9 * len(ks)))
        for stage in ("first_stage", "reranked"):
            print("    " + stage.ljust(16) + "".join(f"{r[stage][k][metric]:.3f}".rjust(9) for k in ks))
        deltas = []
        for k in ks:
            d = res["delta"][f"{metric}@{k}"]
            rel = f"{d['rel_delta_pct']:+.1f}%" if d["rel_delta_pct"] is not None else "n/a"
            deltas.append(f"@{k} {d['abs_delta']:+.3f} ({rel})")
        print("    Δ rerank         " + "   ".join(deltas))


def main(argv: list[str] | None = None) -> int:
    import json as _json
    import sys

    which = (argv or ["all"])[0] if argv is not None else (sys.argv[1:] or ["all"])[0]
    out = {}
    if which in ("all", "ioda"):
        r = run_ioda_rerank_comparison()
        _print_rerank(r); out["ioda"] = r
    if which in ("all", "skill"):
        r = run_skill_rerank_comparison()
        _print_rerank(r); out["skill"] = r
    if which in ("all", "scifact"):
        try:
            r = run_scifact_rerank_comparison()
            _print_rerank(r); out["scifact"] = r
        except RuntimeError as e:
            print(f"\n[beir-scifact] SKIPPED — public benchmark unavailable: {e}")
            print("  Reporting (a) IODA and (b) FortiGate only, as instructed.")
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / "rerank_result.json").write_text(_json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
