"""Evidence-retrieval eval on the REAL IODA v2 three-source pool — LLM-free, zero-dep.

The retrieval task the RCA agent actually faces at ingest time: given an *outage
event* an operator is looking at (a country/ASN, an outage type/cause, and a time
window), surface — out of a single shared pool of 8542 real evidence records
(Cloudflare Radar + IODA v2 active-probing/BGP + RouteViews/RIS BGP) — the records
that actually belong to that event.

    QUERY  = one event (radar:NNN) rendered from operator-observable attributes only
    CORPUS = the 8542-record evidence pool (every record is one document)
    TRUTH  = the records whose ``candidate_event_id`` == this event's ``event_id``

Four retrievers are compared on identical inputs (plus one diagnostic):
  * ``naive``      — bag-of-words overlap fraction (the straw-man baseline);
  * ``bm25``       — Okapi BM25 sparse lexical (:mod:`core.memory.bm25`);
  * ``structured`` — typed multi-axis match: entity_id (typed country/ASN) + the
                     operator time window + entity_type + a coarse source hint;
  * ``rrf``        — Reciprocal Rank Fusion of bm25 + structured (:mod:`core.memory.rrf`);
  * ``structured_no_time`` (diagnostic) — the structured retriever with the time
                     axis removed, to isolate exactly how much the time window buys.

Query-expansion modes (base / stem / expand) come from :mod:`core.memory.query_expansion`
and a CRAG-style confidence gate (:mod:`core.memory.crag_gate`) reports the
abstain/answer/widen split over the BM25 route. Everything is deterministic —
nothing here downloads a model or calls an LLM.

────────────────────────────────────────────────────────────────────────────────
HONESTY / NO-LEAKAGE DESIGN (a skeptical reader should check these):

1. The query for an event is built ONLY from operator-observable manifest fields:
   ``locations``, ``asns``, ``entity_type``, ``outage_type``, ``outage_cause``,
   ``ioda_v2_datasources`` (coarse source hint) and the ``event_start/event_end``
   time window. It never contains ``event_id`` / ``radar_event_id`` /
   ``candidate_event_id`` / ``evidence_id`` / the evidence ``signal_type`` ids.
   (Verified: 0/832 events have a query token equal to their ``radar_event_id``.)

2. Corpus documents are built from non-identifying evidence fields (entity_id,
   entity_type, source, signal_type, phase, topology). The label fields
   ``candidate_event_id`` / ``evidence_id`` / ``raw_ref.record_id`` — which embed
   the event number — are NEVER put into document text.

3. Document ids are content hashes of the evidence_id, NOT the evidence_id itself.
   The evidence_id embeds the event number (``radar:225::radar_outage::onset``);
   using it as the ranking id would let lexical tie-breaks sort an event's own
   evidence first. Hashing makes tie-breaks neutral w.r.t. the query event.

4. ``outage_type`` / ``outage_cause`` are QUERY-ONLY vocabulary — no evidence
   record carries an outage-cause field — so they cannot lexically match and
   cannot be structured-matched without leakage. Lexical retrieval therefore
   collapses to entity (country-code / ASN) overlap; that is a real finding, not
   a bug, and it is why the operator time window (structured) is the useful lever.

5. Structured matches entities *typed* (an ASN query token never matches a
   country document that happens to share the digits), which neutralises the few
   numeric collisions between ASN strings and radar ids.
"""
from __future__ import annotations

import bisect
import functools
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.memory.bm25 import BM25Index, tokenize
from core.memory.crag_gate import crag_gate
from core.memory.query_expansion import make_transform
from core.memory.rrf import rrf_fuse

# ── configuration ──────────────────────────────────────────────────────────────
_DEFAULT_DATA_DIR = Path(
    "/data/netops-runtime/section45_real_internet/"
    "radar_ioda_v2_strict_three_source_with_controls"
)
_MANIFEST_NAME = "radar_ioda_v2_event_manifest.json"
_EVIDENCE_NAME = "radar_ioda_v2_evidence_pool.jsonl"

_K_VALUES = (1, 5, 10)
_HEADLINE_K = 10
_METHODS = ("naive", "bm25", "structured", "rrf")

# Structured-match axis weights. Round, documented, NOT tuned to a metric: entity
# and time are the join keys with real discriminating power; entity_type and the
# coarse source hint only re-rank docs already matched on entity or time.
_W_ENTITY, _W_TIME, _W_TYPE, _W_HINT = 3.0, 2.0, 0.5, 0.5

# CRAG gate thresholds — illustrative fixed defaults (BM25 top-1 median ≈ 13 on
# this pool), reported not tuned. See :func:`crag_gate_summary`.
_CRAG_HI, _CRAG_LO = 3.0, 1.0


def _data_dir(path: str | Path | None) -> Path:
    return Path(path or os.environ.get("IODA_DATA_DIR") or _DEFAULT_DATA_DIR)


# ── loading (cached; the pool is 8542 lines) ────────────────────────────────────
@functools.lru_cache(maxsize=4)
def load_events(path: str | Path | None = None) -> tuple[dict, ...]:
    manifest = json.loads((_data_dir(path) / _MANIFEST_NAME).read_text(encoding="utf-8"))
    return tuple(manifest["events"])


@functools.lru_cache(maxsize=4)
def load_evidence(path: str | Path | None = None) -> tuple[dict, ...]:
    text = (_data_dir(path) / _EVIDENCE_NAME).read_text(encoding="utf-8")
    return tuple(json.loads(line) for line in text.splitlines() if line.strip())


def _doc_id(evidence_id: str) -> str:
    """Content-hash id (see design note 3) — neutral, deterministic tie-breaks."""
    return "d" + hashlib.sha1(evidence_id.encode("utf-8")).hexdigest()[:12]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ── corpus / query construction (non-leaking; see module docstring) ─────────────
def _evidence_doc_text(rec: dict) -> str:
    """Lexical document text from NON-identifying evidence fields only."""
    parts = [
        str(rec["entity_id"]),
        str(rec["entity_type"]),
        str(rec["source"]).replace("_", " "),
        str(rec["signal_type"]).replace("_", " ").replace("-", " "),
        str(rec["phase"]),
    ]
    topo = rec.get("topology") or {}
    if topo.get("origin_as"):
        parts.append(str(topo["origin_as"]))
    if topo.get("collector"):
        parts.append(str(topo["collector"]).replace("+", " ").replace("_", " "))
    return " ".join(parts)


def _evidence_source_tokens(rec: dict) -> frozenset[str]:
    """Coarse source/signal tokens ({'bgp','ping','slash24',...}) for hint matching."""
    return frozenset(tokenize(f"{rec['source']} {rec['signal_type']}".replace("-", " ")))


class Corpus:
    """The evidence pool prepared once for a given document transform (mode)."""

    def __init__(self, evidence: tuple[dict, ...], doc_transform: Callable[[list[str]], list[str]]):
        self.doc_tokens: dict[str, list[str]] = {}
        self.doc_sets: dict[str, set[str]] = {}
        self.inverted: dict[str, set[str]] = {}
        self.doc_to_event: dict[str, str] = {}
        # structured per-doc profile: (entity_lower, entity_type, source_tokens, time_dt)
        self.struct: dict[str, tuple[str, str, frozenset[str], datetime]] = {}
        self.entity_index: dict[tuple[str, str], list[str]] = {}
        time_pairs: list[tuple[datetime, str]] = []

        for rec in evidence:
            did = _doc_id(rec["evidence_id"])
            if did in self.doc_to_event:  # hash collision guard (design note 3)
                raise ValueError(f"doc-id collision on {rec['evidence_id']!r}")
            toks = doc_transform(tokenize(_evidence_doc_text(rec)))
            self.doc_tokens[did] = toks
            self.doc_sets[did] = set(toks)
            for t in self.doc_sets[did]:
                self.inverted.setdefault(t, set()).add(did)
            self.doc_to_event[did] = rec["candidate_event_id"]

            entity = str(rec["entity_id"]).lower()
            etype = str(rec["entity_type"]).lower()
            tdt = _parse_ts(rec["time_bin"])
            self.struct[did] = (entity, etype, _evidence_source_tokens(rec), tdt)
            self.entity_index.setdefault((etype, entity), []).append(did)
            time_pairs.append((tdt, did))

        time_pairs.sort(key=lambda p: (p[0], p[1]))
        self._time_sorted_ts = [p[0] for p in time_pairs]
        self._time_sorted_id = [p[1] for p in time_pairs]
        self.bm25 = BM25Index(self.doc_tokens)

    def lexical_candidates(self, query_tokens: list[str]) -> set[str]:
        cand: set[str] = set()
        for t in set(query_tokens):
            cand |= self.inverted.get(t, set())
        return cand

    def docs_in_window(self, start: datetime, end: datetime) -> list[str]:
        lo = bisect.bisect_left(self._time_sorted_ts, start)
        hi = bisect.bisect_right(self._time_sorted_ts, end)
        return self._time_sorted_id[lo:hi]


class Query:
    """One event rendered as (lexical tokens, structured profile) — operator-observable only."""

    __slots__ = ("event_id", "lex", "countries", "asns", "types", "hints", "start", "end")

    def __init__(self, event: dict, q_transform: Callable[[list[str]], list[str]]):
        self.event_id: str = event["event_id"]
        locations = [str(x) for x in (event.get("locations") or [])]
        asns = [str(x) for x in (event.get("asns") or [])]
        self.countries: frozenset[str] = frozenset(c.lower() for c in locations)
        self.asns: frozenset[str] = frozenset(a.lower() for a in asns)
        self.types: frozenset[str] = frozenset(
            ({"country"} if locations else set()) | ({"asn"} if asns else set())
        )
        self.hints: frozenset[str] = frozenset(
            tokenize(" ".join(str(d).replace("-", " ") for d in (event.get("ioda_v2_datasources") or [])))
        )
        self.start: datetime = _parse_ts(event["event_start"])
        self.end: datetime = _parse_ts(event["event_end"])
        # lexical query text: entity ids + outage type/cause. The bare entity-TYPE
        # words ("country"/"asn") are deliberately NOT emitted: they sit in ~80% of
        # the pool (near-zero IDF, no signal about *which* entity) and would only add
        # uniform noise; entity_type is used properly as a typed structured axis
        # instead. The outage words ARE included (so the straw-man naive retriever
        # sees the operator's real question) but are query-only vocab — design
        # note 4 — so they match nothing and cannot change the ranking.
        lex_parts = list(locations) + list(asns)
        lex_parts.append(str(event.get("outage_type") or "").replace("_", " "))
        lex_parts.append(str(event.get("outage_cause") or "").replace("_", " "))
        self.lex: list[str] = q_transform(tokenize(" ".join(lex_parts)))


# ── retrievers: each maps (Query, k) -> ranked list of doc ids ──────────────────
def _naive_retriever(corpus: Corpus) -> Callable[[Query, int], list[str]]:
    def retrieve(q: Query, k: int) -> list[str]:
        if k <= 0:
            return []
        qterms = set(q.lex)
        if not qterms:
            return []
        scored = []
        for did in corpus.lexical_candidates(q.lex):
            overlap = len(qterms & corpus.doc_sets[did])
            if overlap:
                scored.append((overlap / len(qterms), did))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [d for _, d in scored[:k]]

    return retrieve


def _bm25_retriever(corpus: Corpus) -> Callable[[Query, int], list[str]]:
    def retrieve(q: Query, k: int) -> list[str]:
        if k <= 0 or not q.lex:
            return []
        # reuse BM25Index.score, but only over docs sharing a query term (the rest
        # score 0 and would be dropped anyway) — exact and fast on 8542 docs.
        scored = []
        for did in corpus.lexical_candidates(q.lex):
            s = corpus.bm25.score(q.lex, did)
            if s > 0.0:
                scored.append((s, did))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [d for _, d in scored[:k]]

    return retrieve


def _structured_score(corpus: Corpus, q: Query, did: str, *, use_time: bool) -> float:
    entity, etype, src_tokens, tdt = corpus.struct[did]
    entity_match = (etype == "country" and entity in q.countries) or (
        etype == "asn" and entity in q.asns
    )
    time_match = use_time and (q.start <= tdt <= q.end)
    base = _W_ENTITY * entity_match + _W_TIME * time_match
    if base == 0.0:  # matched neither join key -> not a candidate
        return 0.0
    type_match = etype in q.types
    hint_match = bool(q.hints & src_tokens)
    return base + _W_TYPE * type_match + _W_HINT * hint_match


def _structured_retriever(corpus: Corpus, *, use_time: bool) -> Callable[[Query, int], list[str]]:
    def retrieve(q: Query, k: int) -> list[str]:
        if k <= 0:
            return []
        cand: set[str] = set()
        for etype, ids in (("country", q.countries), ("asn", q.asns)):
            for ent in ids:
                cand.update(corpus.entity_index.get((etype, ent), ()))
        if use_time:
            cand.update(corpus.docs_in_window(q.start, q.end))
        scored = []
        for did in cand:
            s = _structured_score(corpus, q, did, use_time=use_time)
            if s > 0.0:
                scored.append((s, did))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [d for _, d in scored[:k]]

    return retrieve


def build_retrievers(mode: str = "base", path: str | Path | None = None) -> dict[str, Callable[[Query, int], list[str]]]:
    """Build every retriever under a query-expansion ``mode`` (base/stem/expand).

    Only the lexical retrievers (and the RRF that fuses BM25) depend on ``mode``;
    the structured retriever matches raw typed identifiers and is mode-invariant.
    """
    q_transform, d_transform = make_transform(mode)
    corpus = build_corpus(mode, path)
    naive = _naive_retriever(corpus)
    bm25 = _bm25_retriever(corpus)
    structured = _structured_retriever(corpus, use_time=True)
    structured_no_time = _structured_retriever(corpus, use_time=False)

    def rrf(q: Query, k: int) -> list[str]:
        pool = max(k, _HEADLINE_K)
        return rrf_fuse([bm25(q, pool), structured(q, pool)], k)

    return {
        "naive": naive,
        "bm25": bm25,
        "structured": structured,
        "structured_no_time": structured_no_time,
        "rrf": rrf,
    }


@functools.lru_cache(maxsize=8)
def build_corpus(mode: str = "base", path: str | Path | None = None) -> Corpus:
    _, d_transform = make_transform(mode)
    return Corpus(load_evidence(path), d_transform)


def build_queries(mode: str = "base", path: str | Path | None = None, max_events: int | None = None) -> list[Query]:
    q_transform, _ = make_transform(mode)
    events = load_events(path)
    if max_events is not None:
        events = events[:max_events]
    return [Query(e, q_transform) for e in events]


# ── scoring ─────────────────────────────────────────────────────────────────────
def _relevant_sets(path: str | Path | None = None) -> dict[str, set[str]]:
    rel: dict[str, set[str]] = {}
    for rec in load_evidence(path):
        rel.setdefault(rec["candidate_event_id"], set()).add(_doc_id(rec["evidence_id"]))
    return rel


def _score_one(retrieved: list[str], relevant: set[str], k: int) -> dict:
    hits = [r for r in retrieved if r in relevant]
    return {
        "recall_at_k": len(hits) / len(relevant) if relevant else 0.0,
        "false_retrieval": (len(retrieved) - len(hits)) / max(1, len(retrieved)),
    }


def run_ioda_retrieval_eval(
    mode: str = "base",
    path: str | Path | None = None,
    max_events: int | None = None,
    k_values: tuple[int, ...] = _K_VALUES,
) -> dict:
    """Score every retriever at each k over the events (macro-averaged, deterministic).

    ``max_events`` (for fast tests) keeps the first N events in manifest order.
    Retrieval is done once at ``max(k_values)`` per (method, event) and sliced,
    which is exact because every retriever returns a stable top-k prefix.
    """
    queries = build_queries(mode, path, max_events)
    retrievers = build_retrievers(mode, path)
    relevant = _relevant_sets(path)
    k_max = max(k_values)

    methods = tuple(retrievers)  # includes the diagnostic
    out: dict = {
        "dataset_kind": "real-ioda-v2-three-source",
        "mode": mode,
        "n_queries": len(queries),
        "n_corpus_docs": len(build_corpus(mode, path).doc_tokens),
        "k_values": list(k_values),
        "oracle_recall_at_k": {}, "methods": {m: {k: {} for k in k_values} for m in methods},
    }
    # oracle ceiling: a perfect retriever is still capped by relevant-set size.
    for k in k_values:
        vals = [min(k, len(relevant[q.event_id])) / len(relevant[q.event_id]) for q in queries]
        out["oracle_recall_at_k"][k] = round(sum(vals) / len(vals), 4) if vals else 0.0

    for method, retrieve in retrievers.items():
        acc = {k: {"recall_at_k": 0.0, "false_retrieval": 0.0} for k in k_values}
        for q in queries:
            rel = relevant[q.event_id]
            ranked = retrieve(q, k_max)
            for k in k_values:
                row = _score_one(ranked[:k], rel, k)
                acc[k]["recall_at_k"] += row["recall_at_k"]
                acc[k]["false_retrieval"] += row["false_retrieval"]
        n = len(queries)
        for k in k_values:
            out["methods"][method][k] = {
                "recall_at_k": round(acc[k]["recall_at_k"] / n, 4) if n else 0.0,
                "false_retrieval": round(acc[k]["false_retrieval"] / n, 4) if n else 0.0,
            }
    return out


def crag_gate_summary(
    mode: str = "base",
    path: str | Path | None = None,
    max_events: int | None = None,
    *,
    hi: float = _CRAG_HI,
    lo: float = _CRAG_LO,
    k: int = _HEADLINE_K,
) -> dict:
    """Distribution of CRAG gate actions over the BM25 route (reused, not tuned).

    For each event we score BM25, feed the scored list to :func:`crag_gate`, and
    tally correct (answer) / ambiguous (widen) / incorrect (abstain). Demonstrates
    the model-free abstention path — e.g. the entity-less events on which BM25 has
    nothing to ground return an explicit ``not_observed`` abstention.
    """
    corpus = build_corpus(mode, path)
    queries = build_queries(mode, path, max_events)
    actions: dict[str, int] = {"correct": 0, "ambiguous": 0, "incorrect": 0}
    reasons: dict[str, int] = {}
    for q in queries:
        scored = [
            (did, corpus.bm25.score(q.lex, did))
            for did in corpus.lexical_candidates(q.lex)
        ] if q.lex else []
        scored = sorted((p for p in scored if p[1] > 0.0), key=lambda p: (-p[1], p[0]))
        decision = crag_gate(scored, k, hi=hi, lo=lo)
        actions[decision.action] += 1
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    return {"mode": mode, "hi": hi, "lo": lo, "k": k,
            "n_queries": len(queries), "actions": actions, "reasons": reasons}


# ── dataset statistics (honest corpus / ground-truth accounting) ────────────────
def dataset_stats(path: str | Path | None = None) -> dict:
    events = load_events(path)
    evidence = load_evidence(path)
    man = {e["event_id"]: e for e in events}
    rel = _relevant_sets(path)

    sizes = sorted(len(rel[e["event_id"]]) for e in events if e["event_id"] in rel)
    n = len(sizes)
    events_with_ev = sum(1 for e in events if rel.get(e["event_id"]))

    # per-event distinct evidence sources -> single vs multi source
    src_by_event: dict[str, set[str]] = {}
    for rec in evidence:
        src_by_event.setdefault(rec["candidate_event_id"], set()).add(rec["source"])
    src_dist: dict[int, int] = {}
    for s in src_by_event.values():
        src_dist[len(s)] = src_dist.get(len(s), 0) + 1

    # entity reuse across events (why entity-only retrieval over-retrieves)
    loc_to_events: dict[str, set[str]] = {}
    for e in events:
        for loc in (e.get("locations") or []):
            loc_to_events.setdefault(loc, set()).add(e["event_id"])
    shared_locations = sum(1 for evs in loc_to_events.values() if len(evs) > 1)

    # join-key coverage of the pool
    in_window = 0
    entity_hit = 0
    for rec in evidence:
        e = man[rec["candidate_event_id"]]
        if _parse_ts(e["event_start"]) <= _parse_ts(rec["time_bin"]) <= _parse_ts(e["event_end"]):
            in_window += 1
        ent = set(str(x) for x in (e.get("locations") or [])) | set(
            str(x) for x in (e.get("asns") or [])
        )
        if str(rec["entity_id"]) in ent:
            entity_hit += 1

    entityless = sum(1 for e in events if not (e.get("locations") or e.get("asns")))

    def _class_counts(field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in events:
            out[str(e.get(field))] = out.get(str(e.get(field)), 0) + 1
        return out

    return {
        "n_events": len(events),
        "n_corpus_docs": len(evidence),
        "events_with_ge1_evidence": events_with_ev,
        "events_with_0_evidence": len(events) - events_with_ev,
        "relevant_set_size": {
            "min": sizes[0], "max": sizes[-1],
            "mean": round(sum(sizes) / n, 2), "median": sizes[n // 2],
        },
        "sources_per_event": {  # 1 == single-source, 3 == full three-source
            "single_source": src_dist.get(1, 0),
            "two_source": src_dist.get(2, 0),
            "three_source": src_dist.get(3, 0),
        },
        "event_class": _class_counts("event_class"),
        "distinct_locations": len(loc_to_events),
        "locations_shared_by_multiple_events": shared_locations,
        "entityless_events": entityless,
        "pool_time_in_window": {"n": in_window, "frac": round(in_window / len(evidence), 4)},
        "pool_entity_matches_event": {"n": entity_hit, "frac": round(entity_hit / len(evidence), 4)},
    }


# ── reporting ───────────────────────────────────────────────────────────────────
def _print_stats(st: dict) -> None:
    r = st["relevant_set_size"]
    s = st["sources_per_event"]
    print("dataset: REAL IODA v2 strict three-source pool (LLM-free, deterministic)")
    print(f"  corpus       : {st['n_corpus_docs']} evidence records")
    print(f"  queries      : {st['n_events']} events  "
          f"({st['events_with_ge1_evidence']} with >=1 evidence, {st['events_with_0_evidence']} without)")
    print(f"  relevant set : size min {r['min']}  median {r['median']}  "
          f"mean {r['mean']}  max {r['max']}  (so recall@1 is capped ~1/{r['mean']:.0f})")
    print(f"  source mix   : {s['single_source']} single-source  {s['two_source']} two-source  "
          f"{s['three_source']} three-source")
    print(f"  event_class  : {st['event_class']}")
    print(f"  entity reuse : {st['distinct_locations']} distinct locations, "
          f"{st['locations_shared_by_multiple_events']} shared by >1 event  "
          f"(-> entity-only retrieval over-retrieves)")
    print(f"  entityless   : {st['entityless_events']} events have no location/ASN "
          f"(lexical query is empty -> only time can retrieve them)")
    print(f"  join coverage: time-in-window {st['pool_time_in_window']['frac']:.3f} of pool, "
          f"entity-match {st['pool_entity_matches_event']['frac']:.3f} of pool "
          f"(neither is 1.0 -> structured is not a trivial oracle)")


def _print_eval(res: dict) -> None:
    ks = res["k_values"]
    print(f"\nrecall@k / false-retrieval@k, macro-avg over {res['n_queries']} events "
          f"(mode={res['mode']}, corpus={res['n_corpus_docs']}):")
    header = "method".ljust(20) + "".join(f"R@{k}".rjust(8) for k in ks) + "   " + \
             "".join(f"FR@{k}".rjust(8) for k in ks)
    print(header)
    print("-" * len(header))
    oracle = "oracle-ceiling".ljust(20) + "".join(f"{res['oracle_recall_at_k'][k]:.3f}".rjust(8) for k in ks)
    print(oracle + "   " + "".join("-".rjust(8) for _ in ks))
    for method in ("naive", "bm25", "structured", "rrf", "structured_no_time"):
        by_k = res["methods"][method]
        row = method.ljust(20)
        row += "".join(f"{by_k[k]['recall_at_k']:.3f}".rjust(8) for k in ks)
        row += "   " + "".join(f"{by_k[k]['false_retrieval']:.3f}".rjust(8) for k in ks)
        print(row)
    hk = _HEADLINE_K if _HEADLINE_K in ks else ks[-1]
    m = {x: res["methods"][x][hk]["recall_at_k"] for x in ("naive", "bm25", "structured", "rrf", "structured_no_time")}
    print(f"\nheadline (recall@{hk}): naive {m['naive']:.3f} -> bm25 {m['bm25']:.3f} -> "
          f"structured {m['structured']:.3f} -> rrf {m['rrf']:.3f}   "
          f"(structured_no_time {m['structured_no_time']:.3f}: the time axis is worth "
          f"{m['structured'] - m['structured_no_time']:+.3f})")


def _print_modes(path: str | Path | None = None) -> None:
    print(f"\nquery-expansion effect on recall@{_HEADLINE_K} (base/stem/expand):")
    for mode in ("base", "stem", "expand"):
        res = run_ioda_retrieval_eval(mode, path)
        m = {x: res["methods"][x][_HEADLINE_K]["recall_at_k"] for x in _METHODS}
        print(f"  {mode:<7} naive {m['naive']:.3f}  bm25 {m['bm25']:.3f}  "
              f"structured {m['structured']:.3f}  rrf {m['rrf']:.3f}")
    print("  (structured is mode-invariant by construction — it matches raw typed ids)")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else None
    _print_stats(dataset_stats(path))
    _print_eval(run_ioda_retrieval_eval("base", path))
    _print_modes(path)
    gate = crag_gate_summary("base", path)
    print(f"\nCRAG gate over BM25 route (hi={gate['hi']}, lo={gate['lo']}, illustrative fixed "
          f"thresholds): {gate['actions']}")
    print(f"  reasons: {gate['reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
