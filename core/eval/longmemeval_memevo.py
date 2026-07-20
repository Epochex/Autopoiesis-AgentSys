"""LongMemEval measurement of two WIRED memory-management mechanisms.

This harness measures — fairly, on the real LongMemEval-500 — what the kernel's two
newly-wired memory-management paths buy over naive management. Neither mechanism is a
novel algorithm; both are textbook ideas (capacity-budgeted utility eviction; conflict/
supersede-aware update). The contribution here is the *wired integration* plus an honest,
apples-to-apples comparison:

  BUILD 1 — utility-driven eviction (core.evolve.memory_ops.utility_evict)
      Under a capacity budget B (B << sessions/item, so eviction BINDS), which policy
      keeps the answer-bearing session best? utility vs LRU vs Ebbinghaus-time-decay vs
      random. Same items, same B, same embedder-free retriever, same metric.

  BUILD 2 — conflict-resolving UPDATE (core.evolve.memory_ops.supersede)
      On the knowledge-update subset (a fact changes across sessions; the CURRENT answer
      is the latest), does retiring the superseded session help surface the LATEST correct
      answer, vs naive append that keeps both and lets the stale one crowd the slot?

Fairness / anti-rigging:
  * The mechanisms never see answer_session_ids or the answer string. Utility signals are
    computed from the corpus alone (idf salience, A-MEM link degree, self-probe access
    frequency, recency); supersede fires on temporal mutual-nearest-neighbour topic
    similarity. Both decide BEFORE the test query is known.
  * Every condition is cut to the SAME budget B / uses the SAME retriever & k.
  * Thresholds are fixed defaults, documented, and NOT tuned on the recall label
    (sensitivity is reported so the reader can see the knob).

Run:  python -m core.eval.longmemeval_memevo <longmemeval_s_uniqids.json>
"""
from __future__ import annotations

import datetime
import json
import math
import random
import sys

from core.eval.longmemeval import _terms, load_longmemeval
from core.evolve.memory_ops import supersede, utility_evict
from core.memory.store import MemoryRecord, TieredMemoryStore

# ── data helpers ──────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime.datetime:
    head = str(s).split(" (")[0].strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(head, fmt)
        except ValueError:
            continue
    return datetime.datetime.min


def _sess_text(item: dict, n: int) -> str:
    turns = item["haystack_sessions"][n]
    if isinstance(turns, dict):
        turns = turns.get("turns", [])
    return " ".join(str(t.get("content", "")) for t in turns if isinstance(t, dict))


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


# ── store construction + corpus-only utility signals ──────────────────────────

def build_store(item: dict) -> tuple[TieredMemoryStore, dict[str, str], dict[str, int]]:
    """One session -> one episodic memory. Returns (store, mid->sid, mid->recency_rank).
    recency_rank 0 = oldest session by haystack date."""
    mem = TieredMemoryStore()
    sids = item["haystack_session_ids"]
    dates = item["haystack_dates"]
    n = len(sids)
    order = sorted(range(n), key=lambda i: _parse_date(dates[i]))
    rank = {i: r for r, i in enumerate(order)}          # session index -> recency rank
    mid_to_sid: dict[str, str] = {}
    mid_to_rank: dict[str, int] = {}
    for i, sid in enumerate(sids):
        txt = _sess_text(item, i)
        mid = f"m-{sid}"
        rec = MemoryRecord(memory_id=mid, tier="episodic", text=txt, tags=_terms(txt)[:48])
        rec.strength = (rank[i] + 1) / n                # recency proxy: newer -> higher
        mem.add(rec)
        mid_to_sid[mid] = sid
        mid_to_rank[mid] = rank[i]
    return mem, mid_to_sid, mid_to_rank


def populate_signals(mem: TieredMemoryStore, *, link_thresh: float = 0.20,
                     probe_k: int = 5, probe_terms: int = 8) -> None:
    """Fill importance / access_count / links from the CORPUS only (no query, no label).

      importance  = mean idf of the session's salient terms (rare-entity salience)
      links       = A-MEM edges to sessions with tag-Jaccard >= link_thresh (centrality)
      access_count= self-probe frequency: each session's top-idf terms are a proxy query;
                    every memory it retrieves (bar itself) gets +1 — 'how many queries it
                    serves'. Uses corpus sessions as queries, never the test question.
    """
    recs = mem.records()
    n = len(recs)
    tagsets = [set(r.tags) for r in recs]
    df: dict[str, int] = {}
    for ts in tagsets:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + n / c) for t, c in df.items()}
    for rec, ts in zip(recs, tagsets):
        rec.importance = sum(idf[t] for t in ts) / max(1, len(ts))
    # A-MEM links (centrality)
    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(tagsets[i], tagsets[j]) >= link_thresh:
                recs[i].links.append(recs[j].memory_id)
                recs[j].links.append(recs[i].memory_id)
    # self-probe access frequency (tag-overlap top-k; a fast, text-scan-free stand-in for
    # retrieve() — the access signal only needs 'which memories does this probe surface').
    for i, ts in enumerate(tagsets):
        probe = set(sorted(ts, key=lambda t: -idf[t])[:probe_terms])
        scored = sorted(
            ((len(probe & tagsets[j]), j) for j in range(n) if j != i),
            key=lambda x: -x[0],
        )[:probe_k]
        for overlap, j in scored:
            if overlap > 0:
                recs[j].access_count += 1


def clone_store(mem: TieredMemoryStore) -> TieredMemoryStore:
    """Deep copy so each eviction policy starts from the identical populated store."""
    out = TieredMemoryStore()
    for r in mem.records():
        out.add(r.model_copy(deep=True))
    return out


# ── eviction policies (all cut the active store to exactly B survivors) ────────

def evict_to_budget(mem: TieredMemoryStore, budget: int, policy: str, *, seed: int = 0) -> None:
    active = mem.active()
    if len(active) <= budget:
        return
    if policy == "utility":
        utility_evict(mem, budget=budget)               # kernel mechanism under test
        return
    if policy == "random":
        rng = random.Random(seed)
        keep = {r.memory_id for r in rng.sample(active, budget)}
    elif policy == "lru":                               # pure recency: newest B survive
        keep = {r.memory_id for r in sorted(active, key=lambda r: r.strength, reverse=True)[:budget]}
    elif policy == "ebbinghaus":                        # time-decay strength, reset on reuse
        def estr(r: MemoryRecord) -> float:
            return 1.0 if r.access_count > 0 else r.strength
        keep = {r.memory_id for r in sorted(active, key=lambda r: (estr(r), r.strength), reverse=True)[:budget]}
    else:
        raise ValueError(f"unknown policy {policy!r}")
    for r in active:
        if r.memory_id not in keep:
            mem.quarantine(r.memory_id, f"evicted:{policy}")


# ── conflict-resolving supersede (temporal mutual-nearest-neighbour) ───────────

def apply_supersede(mem: TieredMemoryStore, item: dict, *, tau: float = 0.20) -> int:
    """Ingest-time conflict resolution: a later session that is the MUTUAL nearest
    neighbour (same specific topic) of an earlier session, above a tau floor, SUPERSEDEs
    it. Retires the stale prior via the kernel supersede() primitive. Label-free (only
    session text + dates). Returns the number of supersessions fired."""
    sids = item["haystack_session_ids"]
    dates = item["haystack_dates"]
    n = len(sids)
    tagsets = [set(_terms(_sess_text(item, i))) for i in range(n)]
    d = [_parse_date(dates[i]) for i in range(n)]
    nn: dict[int, tuple[int, float]] = {}
    for i in range(n):
        best, bs = -1, -1.0
        for j in range(n):
            if i == j:
                continue
            s = _jaccard(tagsets[i], tagsets[j])
            if s > bs:
                bs, best = s, j
        nn[i] = (best, bs)
    order = sorted(range(n), key=lambda i: d[i])        # temporal ingest order
    fired = 0
    superseded: set[int] = set()
    for i in order:
        j, s = nn[i]
        # i (later) supersedes j (earlier) when they are mutual NNs above the floor.
        if j >= 0 and s >= tau and nn[j][0] == i and d[j] < d[i] and j not in superseded:
            new_mid, old_mid = f"m-{sids[i]}", f"m-{sids[j]}"
            if mem.get(new_mid) and mem.get(old_mid) and not mem.get(old_mid).quarantined:
                supersede(mem, old_mid, mem.get(new_mid))
                superseded.add(j)
                fired += 1
    return fired


# ── scoring ───────────────────────────────────────────────────────────────────

def _retrieve_sids(mem: TieredMemoryStore, item: dict, k: int, mid_to_sid: dict[str, str]) -> tuple[set[str], list[MemoryRecord]]:
    got = mem.retrieve(_terms(str(item.get("question", ""))), [], limit_per_tier=k).get("episodic", [])
    return {mid_to_sid[r.memory_id] for r in got}, got


def _latest_answer_sid(item: dict) -> str | None:
    ans = item.get("answer_session_ids") or []
    if not ans:
        return None
    dates = item["haystack_dates"]
    idx = {s: i for i, s in enumerate(item["haystack_session_ids"])}
    return max(ans, key=lambda a: _parse_date(dates[idx[a]]))


# ── experiments ────────────────────────────────────────────────────────────────

def eviction_experiment(items: list[dict], *, budget: int, k: int,
                        policies=("utility", "ebbinghaus", "lru", "random"),
                        random_seeds=(0, 1, 2)) -> dict:
    """Fair under-budget comparison. Each item: populate corpus signals once, clone, evict
    to `budget` under each policy, retrieve top-k, score recall & answer-session survival."""
    answerable = [it for it in items if it.get("answer_session_ids")]
    agg: dict[str, dict[str, float]] = {p: {"recall": 0.0, "survive": 0.0, "astr": 0.0} for p in policies}
    counts = {p: 0 for p in policies}
    for it in answerable:
        mem0, mid_to_sid, _ = build_store(it)
        populate_signals(mem0)
        ans = set(it["answer_session_ids"])
        astr = str(it.get("answer", "")).strip().lower()
        for p in policies:
            seeds = random_seeds if p == "random" else (0,)
            for sd in seeds:
                mem = clone_store(mem0)
                evict_to_budget(mem, budget, p, seed=sd)
                survivors = {mid_to_sid[r.memory_id] for r in mem.active()}
                rsids, got = _retrieve_sids(mem, it, k, mid_to_sid)
                agg[p]["survive"] += 1.0 if (ans & survivors) else 0.0
                agg[p]["recall"] += 1.0 if (ans & rsids) else 0.0
                agg[p]["astr"] += 1.0 if (astr and any(astr in r.text.lower() for r in got)) else 0.0
                counts[p] += 1
    return {
        "budget": budget, "k": k, "n_items": len(answerable),
        "policies": {
            p: {
                "recall_at_k": round(agg[p]["recall"] / counts[p], 4),
                "answer_survival": round(agg[p]["survive"] / counts[p], 4),
                "answer_string_hit": round(agg[p]["astr"] / counts[p], 4),
            } for p in policies
        },
    }


def conflict_experiment(items: list[dict], *, k: int, tau: float = 0.20) -> dict:
    """Knowledge-update subset: naive append vs conflict-resolving supersede."""
    ku = [it for it in items if it.get("question_type") == "knowledge-update"]
    if not ku:
        return {"n_items": 0, "k": k, "tau": tau, "note": "no knowledge-update items in slice"}
    out: dict[str, dict] = {}
    fired_total = 0
    for cond in ("naive", "supersede"):
        m = {"any_recall": 0, "latest_recall": 0, "answer_string_hit": 0, "stale_only": 0}
        for it in ku:
            mem, mid_to_sid, _ = build_store(it)
            if cond == "supersede":
                fired = apply_supersede(mem, it, tau=tau)
                if cond == "supersede":
                    fired_total += fired
            ans = set(it["answer_session_ids"])
            latest = _latest_answer_sid(it)
            astr = str(it.get("answer", "")).strip().lower()
            rsids, got = _retrieve_sids(mem, it, k, mid_to_sid)
            got_latest = latest in rsids
            got_old = bool((ans - {latest}) & rsids)
            m["any_recall"] += bool(ans & rsids)
            m["latest_recall"] += got_latest
            m["answer_string_hit"] += bool(astr and any(astr in r.text.lower() for r in got))
            m["stale_only"] += (got_old and not got_latest)
        n = len(ku)
        out[cond] = {key: round(v / n, 4) for key, v in m.items()}
    out["n_items"] = len(ku)
    out["k"] = k
    out["tau"] = tau
    out["avg_supersedes_per_item"] = round(fired_total / len(ku), 2)
    return out


def overall_experiment(items: list[dict], *, k: int, budget: int, tau: float = 0.20) -> dict:
    """Headline recall@k on all answerable items: baseline vs +supersede vs +eviction@B
    vs both. Shows what the mechanisms do to the top-line number, not just their subset."""
    answerable = [it for it in items if it.get("answer_session_ids")]
    conds = {c: {"recall": 0, "astr": 0} for c in ("baseline", "supersede", "evict", "both")}
    for it in answerable:
        base, mid_to_sid, _ = build_store(it)
        populate_signals(base)
        ans = set(it["answer_session_ids"])
        astr = str(it.get("answer", "")).strip().lower()
        for c in conds:
            mem = clone_store(base)
            if c in ("supersede", "both"):
                apply_supersede(mem, it, tau=tau)
            if c in ("evict", "both"):
                evict_to_budget(mem, budget, "utility")
            rsids, got = _retrieve_sids(mem, it, k, mid_to_sid)
            conds[c]["recall"] += bool(ans & rsids)
            conds[c]["astr"] += bool(astr and any(astr in r.text.lower() for r in got))
    n = len(answerable)
    return {"k": k, "budget": budget, "tau": tau, "n_items": n,
            "conditions": {c: {"recall_at_k": round(v["recall"] / n, 4),
                               "answer_string_hit": round(v["astr"] / n, 4)} for c, v in conds.items()}}


def full_report(items: list[dict], *, k: int = 5, budgets=(10, 20, 30),
                overall_budget: int = 10, tau: float = 0.20) -> dict:
    """The whole comparison in one pass. Each item's corpus signals are populated ONCE
    and reused across every budget and the overall table (the per-experiment functions
    re-populate; this driver is the fast, canonical reproducer)."""
    answerable = [it for it in items if it.get("answer_session_ids")]
    prepped = []
    for it in answerable:
        mem, mid_to_sid, _ = build_store(it)
        populate_signals(mem)
        prepped.append((it, mem, mid_to_sid))

    def _score(apply_fn) -> dict:
        rec = astr = 0
        for it, mem0, mid_to_sid in prepped:
            mem = clone_store(mem0)
            apply_fn(mem, it)
            ans = set(it["answer_session_ids"])
            a = str(it.get("answer", "")).strip().lower()
            rsids, got = _retrieve_sids(mem, it, k, mid_to_sid)
            rec += bool(ans & rsids)
            astr += bool(a and any(a in r.text.lower() for r in got))
        n = len(prepped)
        return {"recall_at_k": round(rec / n, 4), "answer_string_hit": round(astr / n, 4)}

    eviction: dict[str, dict] = {}
    for b in budgets:
        pol: dict[str, dict] = {}
        for p in ("utility", "ebbinghaus", "lru", "random"):
            seeds = (0, 1, 2) if p == "random" else (0,)
            rec = surv = astr = cnt = 0
            for it, mem0, mid_to_sid in prepped:
                ans = set(it["answer_session_ids"])
                a = str(it.get("answer", "")).strip().lower()
                for sd in seeds:
                    mem = clone_store(mem0)
                    evict_to_budget(mem, b, p, seed=sd)
                    surv += bool(ans & {mid_to_sid[r.memory_id] for r in mem.active()})
                    rsids, got = _retrieve_sids(mem, it, k, mid_to_sid)
                    rec += bool(ans & rsids)
                    astr += bool(a and any(a in r.text.lower() for r in got))
                    cnt += 1
            pol[p] = {"recall_at_k": round(rec / cnt, 4), "answer_survival": round(surv / cnt, 4),
                      "answer_string_hit": round(astr / cnt, 4)}
        eviction[f"B={b}"] = {"budget": b, "k": k, "n_items": len(prepped), "policies": pol}

    def _sup(m, it): apply_supersede(m, it, tau=tau)
    def _ev(m, it): evict_to_budget(m, overall_budget, "utility")
    def _both(m, it): apply_supersede(m, it, tau=tau); evict_to_budget(m, overall_budget, "utility")
    overall = {"baseline": _score(lambda m, it: None), "supersede": _score(_sup),
               f"evict@{overall_budget}": _score(_ev), "both": _score(_both)}

    return {
        "dataset": "longmemeval", "n_answerable": len(answerable), "k": k,
        "baseline_recall_at_k_no_budget": overall["baseline"],
        "eviction_under_budget": eviction,
        "overall_headline": overall,
        "conflict_update_knowledge_update_subset": {
            f"k={kk}": conflict_experiment(items, k=kk, tau=tau) for kk in (1, 2, 3, 5)
        },
        "conflict_tau_sensitivity_at_k1": {
            f"tau={t}": conflict_experiment(items, k=1, tau=t) for t in (0.15, 0.20, 0.25)
        },
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m core.eval.longmemeval_memevo <longmemeval_s_uniqids.json> [k]", file=sys.stderr)
        return 2
    items = load_longmemeval(argv[0])
    k = int(argv[1]) if len(argv) > 1 else 5
    print(json.dumps(full_report(items, k=k), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
