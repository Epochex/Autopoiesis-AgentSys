"""Fair BM25 vs BM25+structure measurement through the REAL TieredMemoryStore.

Every row is scored with the identical harness metric (recall@k over
answer_session_ids, same tokeniser `terms`, same k grid). The ONLY thing that
varies is the ranking function inside core.memory.store.TieredMemoryStore.retrieve:

  * BM25 floor       — rank_bm25.BM25Okapi over terms(text)         (external ref)
  * store / core     — real store, use_structure=False (pure lexical base)
  * store / +recency — real store, strength populated from haystack_dates (Ebbinghaus)
  * store / +central — real store, A-MEM kNN links (forced top-3) -> centrality+importance
  * store / +amem034 — real store, A-MEM links at the SYSTEM threshold 0.34 + recency
  * store / +full    — real store, recency + forced kNN centrality + importance

Structure is derived ONLY from the corpus (session dates, inter-session content
similarity) and the question_date ("now"); never from answer labels. Same k, same
budget, same documents (tags = terms(text)) for every row.
"""
from __future__ import annotations
import sys, json, datetime as dt
from collections import defaultdict
EVAL = "/data/asys-mem/eval_mem_compare"; sys.path.insert(0, EVAL); sys.path.insert(0, "/data/asys-mem")
from harness import terms, session_texts
from rank_bm25 import BM25Okapi
from core.memory.store import MemoryRecord, TieredMemoryStore

KS = [1, 3, 5, 10]

def load_items(p):
    d = json.loads(open(p, encoding="utf-8").read())
    return d.get("items", d) if isinstance(d, dict) else d

def parse_date(s):
    try:
        p = s.split(); y, mo, da = [int(x) for x in p[0].split("/")]
        hh, mm = [int(x) for x in (p[2] if len(p) > 2 else "0:0").split(":")]
        return dt.datetime(y, mo, da, hh, mm)
    except Exception:
        return None

def tag_sim(a, b):  # == core.evolve.memory_ops.similarity with no assets: 0.6 * tag Jaccard
    u = len(a | b)
    return round(0.6 * len(a & b) / u, 4) if u else 0.0

def build_knn_pair(keysets, K=3, thr=0.34):
    """One O(n^2) pass -> (forced top-K kNN graph, top-K-filtered-by-thr graph),
    both bidirectional. keysets are the [:48]-capped term sets (faithful to _ingest)."""
    n = len(keysets); u0 = [set() for _ in range(n)]; ut = [set() for _ in range(n)]
    for i in range(n):
        ai = keysets[i]; cand = []
        for j in range(n):
            if i == j: continue
            s = tag_sim(ai, keysets[j])
            if s > 0: cand.append((s, j))
        cand.sort(reverse=True)
        for s, j in cand[:K]:
            u0[i].add(j); u0[j].add(i)
            if s >= thr: ut[i].add(j); ut[j].add(i)
    return [sorted(x) for x in u0], [sorted(x) for x in ut]

def recency_strength(dates, qdate):
    """Ebbinghaus retrievability in (0,1]: 0.5 ** (age / half_life), half_life =
    the item's median session age (corpus timescale). Newer session -> stronger."""
    ages = []
    for d in dates:
        if d is None or qdate is None: ages.append(None)
        else: ages.append(max(0.0, (qdate - d).total_seconds() / 86400.0))
    valid = sorted(a for a in ages if a is not None)
    hl = valid[len(valid)//2] if valid else 1.0
    hl = hl if hl > 1e-6 else 1.0
    return [1.0 if a is None else 0.5 ** (a / hl) for a in ages]

def prepare(items):
    P = []
    for it in items:
        texts, sids = session_texts(it)
        full = [terms(t) for t in texts]
        keysets = [set(t[:48]) for t in full]   # faithful to _ingest tag cap; fast
        dates = [parse_date(d) for d in it.get("haystack_dates", [])]
        qd = parse_date(it.get("question_date", ""))
        knn0, knn34 = build_knn_pair(keysets, 3, 0.34)
        P.append(dict(
            texts=texts, sids=sids, question=it["question"], qtype=it["question_type"],
            ans=set(i for i, s in enumerate(sids) if s in set(it.get("answer_session_ids") or [])),
            tags=full, tags48=[t[:48] for t in full], knn0=knn0, knn34=knn34,
            strength=recency_strength(dates, qd),
        ))
    return P

def bm25_floor_order(p):
    bm = BM25Okapi(p["tags"]); s = bm.get_scores(terms(p["question"]))
    return sorted(range(len(p["sids"])), key=lambda i: (-s[i], i))

def store_order(p, mode):
    mem = TieredMemoryStore()
    n = len(p["sids"])
    for i in range(n):
        rec = MemoryRecord(memory_id=f"m{i}", tier="episodic", text=p["texts"][i], tags=p["tags48"][i])
        if mode in ("recency", "amem034", "full"):
            rec.strength = p["strength"][i]
        links = []
        if mode in ("central", "full"): links = p["knn0"][i]
        elif mode == "amem034": links = p["knn34"][i]
        if links:
            rec.links = [f"m{j}" for j in links]
            rec.importance = 1.0 + len(links)     # reflection-salience proxy = centrality
        mem.add(rec)
    use = (mode != "core")
    got = mem.retrieve(terms(p["question"]), [], limit_per_tier=10, use_structure=use)
    return [int(r.memory_id[1:]) for r in got["episodic"]]

def evaluate(P, order_fn):
    hits = {k: 0 for k in KS}; scored = 0; bt = {k: defaultdict(list) for k in KS}
    for p in P:
        if not p["ans"]: continue
        scored += 1; order = order_fn(p)
        for k in KS:
            h = int(bool(p["ans"] & set(order[:k]))); hits[k] += h; bt[k][p["qtype"]].append(h)
    rec = {k: round(hits[k]/scored, 4) for k in KS}
    per = {k: {t: round(sum(v)/len(v), 4) for t, v in bt[k].items()} for k in KS}
    return rec, per

def main():
    items = load_items("/data/asys-mem/tmp/longmemeval_s.json")
    print(f"loaded {len(items)} items; preparing structure...", file=sys.stderr)
    P = prepare(items)
    rows = [
        ("BM25 floor (rank_bm25)", lambda p: bm25_floor_order(p)),
        ("store / core (lexical only)", lambda p: store_order(p, "core")),
        ("store / +recency", lambda p: store_order(p, "recency")),
        ("store / +centrality (kNN)", lambda p: store_order(p, "central")),
        ("store / +amem@0.34 +recency", lambda p: store_order(p, "amem034")),
        ("store / +full (recency+central)", lambda p: store_order(p, "full")),
    ]
    out = {}
    for name, fn in rows:
        rec, per = evaluate(P, fn)
        out[name] = {"recall": rec, "by_type": per}
        print(f"{name:34s} r@1={rec[1]:.4f} r@3={rec[3]:.4f} r@5={rec[5]:.4f} r@10={rec[10]:.4f}", file=sys.stderr)
    json.dump(out, open(f"{EVAL}/results_hybrid.json", "w"), indent=2)
    print(f"\nwrote {EVAL}/results_hybrid.json", file=sys.stderr)

if __name__ == "__main__":
    main()
