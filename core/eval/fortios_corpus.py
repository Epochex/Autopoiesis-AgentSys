"""Real FortiOS operations-knowledge-base RAG — structure-aware chunking + deterministic
Contextual Retrieval, with a NON-CIRCULAR, application-grounded eval. OPTIONAL, eval-only.

This is the module behind the resume's "运维知识库 RAG (runbook / 工单 / 设备手册)" claim.
Unlike the other retrieval evals in this package (IODA identifier docs, the 9-skill
catalog), the corpus here is a REAL vendor operations manual — the public **FortiOS 7.4.0
Administration Guide** from docs.fortinet.com — and the queries are the six REAL Dahua→R230
FortiGate incidents from the held-out set. Nothing here is synthesized: the corpus is
downloaded HTML converted to text; the relevance labels are hand-assigned by reading the
manual's section CONTENT (see ``fortios_labels.json``).

Like :mod:`core.eval.dense_retrieval` / :mod:`core.eval.reranker` this is the ONLY-eval,
optional path: it is NEVER imported from the online RCA path, from
``reasoner``/``factory``/orchestrator, or from the default (non-dense) test path. Heavy
deps (sentence-transformers / faiss / torch) are imported lazily and only in the stages
that need them; the corpus build + BM25 stage are pure stdlib.

What it builds
--------------
1. CORPUS. All ~1145 sections of the FortiOS 7.4.0 Administration Guide (docs.fortinet.com).
   Each section page's ``<div id="mc-main-content">`` is parsed to heading-structured text
   (h1..h6 / p / li / table cells) with the stdlib HTML parser — no scraping libraries.
2. CHUNKING. Structure-aware: a section is split at its major (h1/h2) heading boundaries
   and packed into ~``TARGET_CHARS`` windows, so a chunk never straddles two top-level
   headings and always carries its in-page heading trail.
3. CONTEXTUAL RETRIEVAL (deterministic, zero-cost, reproducible — NOT via an LLM). Each
   chunk is prefixed with a document-level context header built purely from its
   *section-title hierarchy*: ``FortiOS 7.4 Administration Guide > <toc ancestors> >
   <section title> > <in-page subheading>``. The hierarchy comes from the guide's own
   table-of-contents tree (nested ``<ul class="toc">``), so it is fully deterministic. This
   is the cheap, reproducible variant of Anthropic's Contextual Retrieval: instead of
   asking an LLM to write a per-chunk context, we lift the ground-truth breadcrumb the
   publisher already encoded in the ToC.
4. PIPELINE. index -> BM25 first stage -> +Contextual-Retrieval -> +dense/hybrid (bge +
   RRF) -> +cross-encoder rerank, measuring recall@k / nDCG@k at each stage (reusing
   :class:`core.memory.bm25.BM25Index`, :class:`core.eval.dense_retrieval.DenseIndex`,
   :class:`core.eval.reranker.CrossEncoderReranker`, :func:`core.memory.rrf.rrf_fuse`).

Non-circularity (the whole point)
---------------------------------
The failure this eval is designed to avoid: labels defined by the retriever's own scoring
key (the IODA ``candidate_event_id`` was a per-event entity+time pull, and the structured
retriever scored on that same key -> a circular +334%). Here the relevance labels are the
*semantic content* of the manual sections: "does this section explain the mechanism behind
this incident's root cause?" — a judgment made by reading prose, with NO reference to any
retriever's term overlap, embedding cosine, or rerank score. The label set is frozen in
``fortios_labels.json`` with a written rationale per (incident, section). See
``why_labels_are_noncircular()`` for the full argument. Because BM25 / dense / rerank all
score on the query↔chunk text and the labels are assigned on the section↔root-cause
*meaning*, no retriever can reconstruct the label key from its own scores.
"""
from __future__ import annotations

import html as _htmlmod  # noqa: F401  (kept for clarity; convert_charrefs handles entities)
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Sequence

_HERE = Path(__file__).resolve()
_CACHE_DIR = Path(os.environ.get("DENSE_CACHE_DIR") or (_HERE.parents[2] / ".dense_cache"))
_LABELS_PATH = _HERE.parent / "fortios_labels.json"

DOC_TITLE = "FortiOS 7.4 Administration Guide"
_GUIDE_BASE = "https://docs.fortinet.com/document/fortigate/7.4.0/administration-guide"
# The guide's landing/first section — its sidebar carries the FULL nested ToC for every page.
_TOC_SEED = f"{_GUIDE_BASE}/954635/getting-started"

# chunking knobs (chars). Structure first, size second: never straddle an h1/h2 boundary.
TARGET_CHARS = 1100
MAX_CHARS = 1800
MIN_SECTION_CHARS = 90        # drop near-empty pages (pure "see the following topics" stubs)

_HEAD_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_MAJOR_HEAD = {"h1", "h2"}    # chunk boundaries
_BLOCK_TAGS = _HEAD_TAGS | {"p", "li", "td", "th", "pre", "dt", "dd", "caption"}
_SKIP_TAGS = {"script", "style", "noscript"}


# ══════════════════════════════════════════════════════════════════════════════════
# 1. ToC hierarchy  (nested <ul class="toc"> -> {section_id: (title, slug, [ancestors])})
# ══════════════════════════════════════════════════════════════════════════════════
class _TocParser(HTMLParser):
    """Parse the guide sidebar's nested ``<ul class="toc">`` tree into a hierarchy map.

    Every section link is rendered twice per page (a desktop + a mobile ToC); we keep the
    occurrence with the LONGEST ancestor chain — the fully-nested desktop one — so the
    breadcrumb is complete.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.stack: list[tuple[int, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._in_a = False
        self._pending_depth = 0
        self.result: dict[str, tuple[str, str, list[str]]] = {}

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "ul" and "toc" in (ad.get("class") or ""):
            self.depth += 1
        elif tag == "a" and "toc" in (ad.get("class") or "") and "/document/" in (ad.get("href") or ""):
            self._href = ad["href"]
            self._buf = []
            self._in_a = True
            self._pending_depth = self.depth

    def handle_data(self, data):
        if self._in_a:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            m = re.search(r"/administration-guide/(\d+)/([a-z0-9-]+)", self._href or "")
            if m and title:
                sid, slug = m.group(1), m.group(2)
                anc = [t for d, t in self.stack if d < self._pending_depth]
                if sid not in self.result or len(anc) > len(self.result[sid][2]):
                    self.result[sid] = (title, slug, anc)
                self.stack = [(d, t) for d, t in self.stack if d < self._pending_depth]
                self.stack.append((self._pending_depth, title))
            self._in_a = False
            self._href = None
        elif tag == "ul" and self.depth > 0:
            self.stack = [(d, t) for d, t in self.stack if d < self.depth]
            self.depth -= 1


def parse_toc(html: str) -> dict[str, tuple[str, str, list[str]]]:
    """``{section_id: (title, slug, [ancestor_titles])}`` from a guide page's sidebar."""
    p = _TocParser()
    p.feed(html)
    return p.result


# ══════════════════════════════════════════════════════════════════════════════════
# 2. Section content  (<div id="mc-main-content"> -> heading-structured blocks)
# ══════════════════════════════════════════════════════════════════════════════════
class _ContentParser(HTMLParser):
    """Capture only the main article region as ordered ``(tag, text)`` blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_main = False
        self._div_scope: list[bool] = []   # is each open <div> within the main-content subtree
        self._cur: str | None = None
        self._buf: list[str] = []
        self._skip = 0
        self.blocks: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "div":
            starts_main = ad.get("id") == "mc-main-content"
            if starts_main:
                self.in_main = True
            self._div_scope.append(bool(starts_main or self.in_main))
        if not self.in_main:
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._cur = tag
            self._buf = []
        elif tag in ("br",):
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if self.in_main and self._skip == 0 and tag in _BLOCK_TAGS:
            self._flush()
        if tag == "div" and self._div_scope:
            self._div_scope.pop()
            if self.in_main and not any(self._div_scope):
                self.in_main = False

    def handle_data(self, data):
        if self.in_main and self._skip == 0 and self._cur is not None:
            self._buf.append(data)

    def _flush(self):
        if self._cur is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.blocks.append((self._cur, text))
        self._cur = None
        self._buf = []


def extract_blocks(html: str) -> list[tuple[str, str]]:
    """Ordered ``(tag, text)`` blocks of a section page's main article (or ``[]``)."""
    i = html.find('id="mc-main-content"')
    if i < 0:
        return []
    start = html.rfind("<div", 0, i)
    p = _ContentParser()
    p.feed(html[start if start >= 0 else i:])
    p._flush()
    return p.blocks


# ══════════════════════════════════════════════════════════════════════════════════
# 3. Structure-aware chunking + deterministic Contextual-Retrieval header
# ══════════════════════════════════════════════════════════════════════════════════
@dataclass
class Chunk:
    id: str                     # "<section_id>#<n>"
    section_id: str
    section_title: str
    toc_path: list[str]         # [DOC_TITLE, *ancestors, section_title]  (the durable hierarchy)
    heading_trail: list[str]    # in-page sub-headings (h2/h3/...) above this chunk
    text: str                   # raw chunk body (no context header)
    context_header: str         # deterministic CR header derived from the hierarchy

    @property
    def cr_text(self) -> str:
        """Contextual-Retrieval text: the deterministic header prepended to the body."""
        return f"{self.context_header}\n\n{self.text}"


def _context_header(toc_path: list[str], heading_trail: list[str]) -> str:
    """Deterministic document-level context: the section-title hierarchy breadcrumb.

    e.g. ``FortiOS 7.4 Administration Guide > User & Authentication > LDAP servers >
    Configuring an LDAP server > To configure an LDAP server on the FortiGate``.
    Built ONLY from the ToC ancestors + section title + in-page subheading — no LLM, no
    corpus statistics, so it is byte-for-byte reproducible.
    """
    parts = list(toc_path)
    for h in heading_trail:
        if not parts or h != parts[-1]:
            parts.append(h)
    return " > ".join(parts)


def chunk_section(
    section_id: str,
    title: str,
    ancestors: Sequence[str],
    blocks: Sequence[tuple[str, str]],
    *,
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
) -> list[Chunk]:
    """Split one section's blocks into structure-aware chunks with CR headers.

    Rule: never let a chunk straddle a major (h1/h2) heading; within a major block, pack
    content until ~``target_chars`` (or a single oversized block), then cut. Each chunk
    records the in-page heading trail (nearest h2/h3/... at its start) for its CR header.
    """
    toc_path = [DOC_TITLE, *[a for a in ancestors], title]
    chunks: list[Chunk] = []
    heading_trail: list[str] = []          # current [h2, h3, ...] below the section h1
    buf: list[str] = []
    buf_len = 0
    buf_trail: list[str] = []              # heading trail captured at buffer start

    def emit():
        nonlocal buf, buf_len, buf_trail
        body = "\n".join(buf).strip()
        if body:
            n = len(chunks)
            chunks.append(Chunk(
                id=f"{section_id}#{n}",
                section_id=section_id,
                section_title=title,
                toc_path=toc_path,
                heading_trail=list(buf_trail),
                text=body,
                context_header=_context_header(toc_path, buf_trail),
            ))
        buf, buf_len = [], 0

    for tag, text in blocks:
        if tag == "h1":
            # section title itself — it's already in the CR header; skip as body noise.
            continue
        if tag in _HEAD_TAGS:
            level = int(tag[1])
            # update the in-page heading trail (keep h2..h4; deeper are procedure titles)
            depth = level - 2  # h2 -> 0
            if depth < 0:
                depth = 0
            heading_trail = heading_trail[:depth]
            heading_trail.append(text)
            if tag in _MAJOR_HEAD and buf:
                emit()
            if not buf:                    # buffer starts here -> record its trail
                buf_trail = list(heading_trail)
            buf.append(text)
            buf_len += len(text)
            continue
        if not buf:
            buf_trail = list(heading_trail)
        buf.append(text)
        buf_len += len(text) + 1
        if buf_len >= target_chars:
            emit()
    if buf:
        emit()
    # merge a trailing tiny chunk into its predecessor to avoid heading-only fragments
    if len(chunks) >= 2 and len(chunks[-1].text) < 60:
        prev, last = chunks[-2], chunks[-1]
        merged = Chunk(prev.id, prev.section_id, prev.section_title, prev.toc_path,
                       prev.heading_trail, prev.text + "\n" + last.text, prev.context_header)
        chunks = chunks[:-2] + [merged]
    return chunks


# ══════════════════════════════════════════════════════════════════════════════════
# 4. Corpus build  (download-or-local -> chunks -> cached JSON)
# ══════════════════════════════════════════════════════════════════════════════════
_HTML_CACHE = _CACHE_DIR / "fortios_html"
_CORPUS_JSON = _CACHE_DIR / "fortios_corpus.json"


def _fetch(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ops-kb-rag-eval/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def download_guide(html_dir: Path = _HTML_CACHE, *, sleep: float = 0.0) -> Path:
    """Download the whole guide's section HTML into ``html_dir`` (idempotent).

    Reproducibility path for a machine without a prebuilt corpus. Sequential + polite;
    the interactive build in this repo used parallel curl, but the result is identical.
    """
    html_dir.mkdir(parents=True, exist_ok=True)
    seed = _fetch(_TOC_SEED)
    (html_dir / "954635.html").write_text(seed, encoding="utf-8")
    toc = parse_toc(seed)
    for sid, (_t, slug, _a) in toc.items():
        dest = html_dir / f"{sid}.html"
        if dest.exists() and dest.stat().st_size > 1000:
            continue
        try:
            page = _fetch(f"{_GUIDE_BASE}/{sid}/{slug}")
        except Exception:  # noqa: BLE001
            continue
        if 'id="mc-main-content"' in page:
            dest.write_text(page, encoding="utf-8")
        if sleep:
            time.sleep(sleep)
    return html_dir


def build_corpus(
    *,
    html_dir: Path = _HTML_CACHE,
    toc_html_path: Path | None = None,
    rebuild: bool = False,
    write_cache: bool = True,
) -> dict:
    """Build (or load) the chunked FortiOS corpus.

    Returns ``{"chunks": [chunk-dict...], "n_sections": int, "source": ...}``. Uses the
    cached JSON if present unless ``rebuild``. Parses local HTML in ``html_dir`` (as
    produced by :func:`download_guide` or the interactive parallel fetch).
    """
    if _CORPUS_JSON.exists() and not rebuild:
        return json.loads(_CORPUS_JSON.read_text(encoding="utf-8"))

    html_dir = Path(html_dir)
    if not html_dir.exists():
        raise RuntimeError(
            f"no HTML at {html_dir} and no cached corpus at {_CORPUS_JSON}; "
            f"run download_guide() first (needs docs.fortinet.com reachable)."
        )
    seed_path = Path(toc_html_path) if toc_html_path else (html_dir / "954635.html")
    toc = parse_toc(seed_path.read_text(encoding="utf-8", errors="replace"))

    chunks: list[Chunk] = []
    n_sections = 0
    skipped: list[str] = []
    for sid, (title, slug, ancestors) in sorted(toc.items()):
        page = html_dir / f"{sid}.html"
        if not page.exists():
            skipped.append(slug)
            continue
        blocks = extract_blocks(page.read_text(encoding="utf-8", errors="replace"))
        body_chars = sum(len(t) for tag, t in blocks if tag != "h1")
        if body_chars < MIN_SECTION_CHARS:
            skipped.append(slug)
            continue
        section_chunks = chunk_section(sid, title, ancestors, blocks)
        if section_chunks:
            chunks.extend(section_chunks)
            n_sections += 1

    out = {
        "source": "docs.fortinet.com FortiOS 7.4.0 Administration Guide",
        "source_base_url": _GUIDE_BASE,
        "doc_title": DOC_TITLE,
        "n_sections": n_sections,
        "n_chunks": len(chunks),
        "n_sections_in_toc": len(toc),
        "n_skipped": len(skipped),
        "chunking": {"target_chars": TARGET_CHARS, "max_chars": MAX_CHARS,
                     "min_section_chars": MIN_SECTION_CHARS,
                     "policy": "split at h1/h2 boundaries; pack to target_chars; CR header from ToC hierarchy"},
        "chunks": [c.__dict__ for c in chunks],
    }
    if write_cache:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CORPUS_JSON.write_text(json.dumps(out), encoding="utf-8")
    return out


def load_corpus() -> dict:
    """Load the cached corpus, or raise a clear error telling the caller how to build it."""
    if not _CORPUS_JSON.exists():
        raise RuntimeError(
            f"no cached FortiOS corpus at {_CORPUS_JSON}; build it with "
            f"`python -m core.eval.fortios_corpus build` (needs the guide HTML or network)."
        )
    return json.loads(_CORPUS_JSON.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════════════
# 5. Labels  (committed non-circular map + optional real held-out query text)
# ══════════════════════════════════════════════════════════════════════════════════
_HELDOUT_PATH = _HERE.parents[2] / "domains" / "network_rca" / "fixtures" / "real" / "heldout_cases.json"


def why_labels_are_noncircular() -> str:
    return (
        "The relevance labels are the SEMANTIC CONTENT of manual sections — 'does this "
        "section explain the mechanism behind this incident's root cause?' — assigned by "
        "reading prose, and frozen with a written rationale in fortios_labels.json. Every "
        "retriever here scores on the query↔chunk TEXT (BM25 term overlap, bge cosine, "
        "cross-encoder pair score); the labels are assigned on the section↔root-cause "
        "MEANING, using neither the query string nor any retriever score. There is no shared "
        "key (no entity+time join, no title match, no term-overlap threshold) a retriever "
        "could reconstruct to 'earn' the label — which is exactly the circularity that "
        "invalidated the IODA +334% (labels there WERE the structured retriever's entity+time "
        "key). A retriever can only be right here by actually matching an operator's incident "
        "wording to a manual section that means the same thing."
    )


def load_labels(*, use_heldout: bool = True) -> list[dict]:
    """Load eval queries + relevant section ids.

    Each item: ``{incident_id, query, relevant_sections: [sid...], rationale, root_cause}``.
    The relevant-section map + rationale come from the committed ``fortios_labels.json``.
    The query text prefers the REAL held-out incident wording (loaded at runtime from the
    gitignored ``heldout_cases.json`` when present) and falls back to the committed authored
    query so the eval still runs on a fresh checkout.
    """
    spec = json.loads(_LABELS_PATH.read_text(encoding="utf-8"))
    heldout_q: dict[str, str] = {}
    if use_heldout and _HELDOUT_PATH.exists():
        try:
            for rec in json.loads(_HELDOUT_PATH.read_text(encoding="utf-8")):
                case = rec.get("case", {})
                if case.get("id") and case.get("query"):
                    heldout_q[case["id"]] = case["query"]
        except Exception:  # noqa: BLE001
            heldout_q = {}
    items = []
    for lab in spec["labels"]:
        iid = lab["incident_id"]
        items.append({
            "incident_id": iid,
            "query": heldout_q.get(iid, lab["query_fallback"]),
            "query_source": "heldout_cases.json" if iid in heldout_q else "authored_fallback",
            "relevant_sections": list(lab["relevant_sections"]),
            "rationale": lab["rationale"],
            "root_cause": lab.get("root_cause", ""),
        })
    return items


# ══════════════════════════════════════════════════════════════════════════════════
# 6. Eval driver  —  BM25(raw) -> +CR -> +hybrid(dense RRF) -> +rerank  (section-level)
# ══════════════════════════════════════════════════════════════════════════════════
def _collapse_to_sections(chunk_ranking: Iterable[str], chunk_to_section: dict[str, str]) -> list[str]:
    """Passage->document: order sections by first appearance in the chunk ranking."""
    seen: set[str] = set()
    out: list[str] = []
    for cid in chunk_ranking:
        sec = chunk_to_section.get(cid)
        if sec is None or sec in seen:
            continue
        seen.add(sec)
        out.append(sec)
    return out


def _stage_delta(base: dict, new: dict, k_values: Sequence[int]) -> dict:
    out = {}
    for metric in ("recall_at_k", "ndcg_at_k"):
        for k in k_values:
            b, n = base[k][metric], new[k][metric]
            out[f"{metric}@{k}"] = {
                "from": round(b, 4), "to": round(n, 4),
                "abs_delta": round(n - b, 4),
                "rel_delta_pct": round((n - b) / b * 100, 1) if b else None,
            }
    return out


def run_fortios_rag_eval(
    *,
    model_name: str = "BAAI/bge-small-en-v1.5",
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    k_values: tuple[int, ...] = (1, 3, 5, 10),
    rerank_depth: int = 30,
    rrf_pool: int = 60,
    include_rerank: bool = True,
    use_heldout: bool = True,
) -> dict:
    """Four-stage RAG eval on the real FortiOS corpus with non-circular incident labels.

    Stages (each adds ONE component; recall@k / nDCG@k measured at every stage, at SECTION
    granularity via passage->document collapse):
      1. ``bm25_raw``   — BM25 over raw chunk text (no context header).
      2. ``bm25_cr``    — BM25 over Contextual-Retrieval text (ToC-hierarchy header prepended).
      3. ``hybrid_cr``  — RRF( BM25-CR , dense-bge-CR ) over the CR chunks.
      4. ``hybrid_rerank`` — cross-encoder reranks the hybrid top-``rerank_depth`` pool.
    """
    from core.memory.bm25 import BM25Index, tokenize
    from core.memory.rrf import rrf_fuse

    corpus = build_corpus() if not _CORPUS_JSON.exists() else load_corpus()
    chunks = corpus["chunks"]
    chunk_ids = [c["id"] for c in chunks]
    chunk_to_section = {c["id"]: c["section_id"] for c in chunks}
    raw_text = {c["id"]: c["text"] for c in chunks}
    cr_text = {c["id"]: f'{c["context_header"]}\n\n{c["text"]}' for c in chunks}

    labels = load_labels(use_heldout=use_heldout)
    # relevant sections that actually exist in the corpus (guard against a skipped page)
    present_sections = set(chunk_to_section.values())
    queries = []
    for lab in labels:
        rel = {s for s in lab["relevant_sections"] if s in present_sections}
        queries.append({**lab, "relevant": rel})
    missing = [(l["incident_id"], sorted(set(l["relevant_sections"]) - present_sections))
               for l in queries if set(l["relevant_sections"]) - present_sections]

    k_max = max(k_values)
    depth = max(rerank_depth, k_max, rrf_pool)

    # ── first stages: BM25 over raw vs CR text ──────────────────────────────────────
    bm25_raw = BM25Index({cid: tokenize(raw_text[cid]) for cid in chunk_ids})
    bm25_cr = BM25Index({cid: tokenize(cr_text[cid]) for cid in chunk_ids})

    # ── dense over CR text (bge) — reuse DenseIndex ─────────────────────────────────
    from core.eval.dense_retrieval import DenseIndex, score_ranking, _macro
    dense_cr = DenseIndex.build(
        chunk_ids, [cr_text[cid] for cid in chunk_ids],
        model_name=model_name, index_type="flat", cache_key=f"fortios_cr_{len(chunk_ids)}",
    )

    reranker = None
    if include_rerank:
        from core.eval.reranker import CrossEncoderReranker
        reranker = CrossEncoderReranker(reranker_model)

    stages = ["bm25_raw", "bm25_cr", "hybrid_cr"] + (["hybrid_rerank"] if include_rerank else [])
    acc: dict[str, dict[int, list[dict]]] = {s: {k: [] for k in k_values} for s in stages}
    per_query: dict[str, dict] = {}
    t_dense = t_rerank = 0.0

    dense_batch = dense_cr.search_texts([q["query"] for q in queries], depth, model_name=model_name)

    for qi, q in enumerate(queries):
        qtext, rel = q["query"], q["relevant"]
        # stage 1/2 first-stage chunk rankings
        raw_rank = [cid for cid, _ in bm25_raw.rank_with_scores(qtext, depth)]
        cr_rank = [cid for cid, _ in bm25_cr.rank_with_scores(qtext, depth)]
        t0 = time.time()
        dense_rank = [cid for cid, _ in dense_batch[qi]]
        t_dense += time.time() - t0
        # stage 3 hybrid: RRF fuse BM25-CR + dense-CR
        hybrid_rank = rrf_fuse([cr_rank, dense_rank], depth)
        stage_rank = {"bm25_raw": raw_rank, "bm25_cr": cr_rank, "hybrid_cr": hybrid_rank}
        # stage 4 rerank the hybrid pool
        if reranker is not None:
            pool = hybrid_rank[:depth]
            cands = [(cid, cr_text[cid]) for cid in pool]
            t0 = time.time()
            stage_rank["hybrid_rerank"] = reranker.rerank(qtext, cands, depth)
            t_rerank += time.time() - t0
        # score each stage at SECTION granularity
        pq = {"query": qtext, "query_source": q["query_source"], "relevant": sorted(rel)}
        for s in stages:
            sec_rank = _collapse_to_sections(stage_rank[s], chunk_to_section)
            for k in k_values:
                acc[s][k].append(score_ranking(sec_rank, rel, k))
            pq[s] = sec_rank[:k_max]
        per_query[q["incident_id"]] = pq

    results = {s: {k: _macro(acc[s][k]) for k in k_values} for s in stages}
    deltas = {
        "cr_over_bm25": _stage_delta(results["bm25_raw"], results["bm25_cr"], k_values),
        "hybrid_over_cr": _stage_delta(results["bm25_cr"], results["hybrid_cr"], k_values),
        "full_over_bm25": _stage_delta(results["bm25_raw"], results[stages[-1]], k_values),
    }
    if include_rerank:
        deltas["rerank_over_hybrid"] = _stage_delta(results["hybrid_cr"], results["hybrid_rerank"], k_values)

    return {
        "dataset_kind": "real-fortios-adminguide-rag",
        "corpus_source": corpus["source"],
        "n_corpus_chunks": len(chunk_ids),
        "n_corpus_sections": corpus["n_sections"],
        "model": model_name,
        "reranker_model": reranker_model if include_rerank else None,
        "n_queries": len(queries),
        "k_values": list(k_values),
        "rerank_depth": depth,
        "stages": stages,
        "results": results,
        "deltas": deltas,
        "labels_are_noncircular": why_labels_are_noncircular(),
        "missing_relevant_sections": missing,
        "seconds": {"dense": round(t_dense, 2), "rerank": round(t_rerank, 2)},
        "per_query": per_query,
        "query_sources": {q["incident_id"]: q["query_source"] for q in queries},
    }


# ── reporting / CLI ────────────────────────────────────────────────────────────────
def _print_eval(res: dict) -> None:
    ks = res["k_values"]
    print("=" * 82)
    print(f"FortiOS ops-KB RAG — {res['n_corpus_chunks']} chunks / {res['n_corpus_sections']} "
          f"sections  |  {res['n_queries']} real R230 incident queries")
    print(f"corpus : {res['corpus_source']}")
    print(f"model  : {res['model']}   reranker: {res['reranker_model']}")
    qs = res["query_sources"]
    n_real = sum(1 for v in qs.values() if v == "heldout_cases.json")
    print(f"queries: {n_real}/{res['n_queries']} use REAL held-out incident text "
          f"({res['n_queries']-n_real} authored fallback)")
    if res["missing_relevant_sections"]:
        print(f"WARNING missing relevant sections: {res['missing_relevant_sections']}")
    for metric, label in (("recall_at_k", "recall@k"), ("ndcg_at_k", "nDCG@k")):
        print(f"\n{label}:")
        print("stage".ljust(16) + "".join(f"@{k}".rjust(9) for k in ks))
        print("-" * (16 + 9 * len(ks)))
        for s in res["stages"]:
            print(s.ljust(16) + "".join(f"{res['results'][s][k][metric]:.3f}".rjust(9) for k in ks))
    print("\nstage deltas (recall@k / nDCG@k):")
    for name, d in res["deltas"].items():
        bits = []
        for k in ks:
            r = d[f"recall_at_k@{k}"]
            rel = f"{r['rel_delta_pct']:+.0f}%" if r["rel_delta_pct"] is not None else "n/a"
            bits.append(f"R@{k} {r['abs_delta']:+.3f}({rel})")
        print(f"  {name:20} " + "  ".join(bits))


def main(argv: list[str] | None = None) -> int:
    import sys
    args = argv if argv is not None else sys.argv[1:]
    cmd = (args or ["eval"])[0]
    if cmd == "download":
        d = download_guide()
        print(f"downloaded guide HTML into {d}")
        return 0
    if cmd == "build":
        out = build_corpus(rebuild=True)
        print(f"built corpus: {out['n_chunks']} chunks / {out['n_sections']} sections "
              f"(of {out['n_sections_in_toc']} ToC entries, {out['n_skipped']} skipped) -> {_CORPUS_JSON}")
        return 0
    # default: run the eval
    include_rerank = "no-rerank" not in args
    res = run_fortios_rag_eval(include_rerank=include_rerank)
    _print_eval(res)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / "fortios_rag_result.json").write_text(json.dumps(res, indent=2))
    print(f"\nwrote {_CACHE_DIR / 'fortios_rag_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
