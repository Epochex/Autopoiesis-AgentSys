"""Run the REAL mem0ai library on LongMemEval-500 through the same shared metric.

Fair, LLM-free, same-embedder configuration
--------------------------------------------
* library : mem0ai (Memory OSS), the real pip package.
* embedder: huggingface provider, sentence-transformers/all-MiniLM-L6-v2 — the
  SAME model id as the flat-vector / Reflexion baselines.
* store   : qdrant, in-memory (on_disk=False).
* infer   : FALSE. Mem0's headline is LLM fact-extraction (add(infer=True)),
  which needs an API key and would break LLM-free apples-to-apples. infer=False
  exercises Mem0's real storage + vector-retrieval pipeline (dedup/hashing,
  metadata, qdrant ANN search) minus the LLM — the fairest comparable mode.
* isolation: one memory per session, namespaced by user_id=item<idx>; search is
  filtered to that item so only its ~50 sessions compete. threshold=0.0, top_k=k
  → pure top-k, no similarity cutoff (mem0's default threshold=0.1 would silently
  drop low-similarity hits and is NOT comparable to the other systems).

Sharding: every LongMemEval item is independent, so items[start:end] can be run
in a separate process with its own in-memory store and the raw counts merged
(merge_mem0.py) to the identical number as one 500-item pass. --start/--end select
the shard. The dummy OPENAI_API_KEY only lets Memory construct; infer=False means
no LLM call is ever made.
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("OPENAI_API_KEY", "sk-dummy-never-called")
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from harness import EMBED_MODEL, finalize, session_texts, tally_raw  # noqa: E402

K_GRID = [1, 3, 5, 10]


class Mem0Retriever:
    name = "Mem0 (mem0ai, infer=False)"

    def __init__(self, qdrant_path=None):
        from mem0 import Memory
        # Disable mem0's sqlite ADD-event history: it is audit-only, is never read by
        # search() (which hits the vector store), and its shared handle both locks
        # under parallel shards and slows every add. Zero effect on retrieval/recall.
        import mem0.memory.storage as _storage
        _storage.SQLiteManager.add_history = lambda self, *a, **k: None
        qcfg = {"collection_name": "lme", "on_disk": False, "embedding_model_dims": 384}
        if qdrant_path:                       # per-shard isolated store (parallel-safe)
            qcfg["path"] = qdrant_path
        cfg = {
            "embedder": {"provider": "huggingface", "config": {"model": EMBED_MODEL}},
            "vector_store": {"provider": "qdrant", "config": qcfg},
        }
        if qdrant_path:                       # isolate the sqlite history db per shard too
            cfg["history_db_path"] = f"{qdrant_path}/history.db"
        self.m = Memory.from_config(cfg)

    def prepare(self, items, base_idx=0):
        t0 = time.time()
        n_add = 0
        for j, item in enumerate(items):
            texts, sids = session_texts(item)
            uid = f"item{base_idx + j}"
            for i, text in enumerate(texts):
                self.m.add([{"role": "user", "content": text}], user_id=uid,
                           infer=False, metadata={"pos": i, "sid": sids[i]})
                n_add += 1
            if (j + 1) % 25 == 0:
                print(f"  [shard {base_idx}] added {n_add} sessions / {j+1} items "
                      f"({time.time()-t0:.0f}s)", file=sys.stderr, flush=True)
        print(f"  [shard {base_idx}] prepare done: {n_add} sessions in {time.time()-t0:.0f}s",
              file=sys.stderr, flush=True)

    def retrieve(self, idx, texts, sids, question, k):
        res = self.m.search(question, top_k=k, filters={"user_id": f"item{idx}"}, threshold=0.0)
        hits = res.get("results", res) if isinstance(res, dict) else res
        out = []
        for h in hits[:k]:
            md = h.get("metadata") or {}
            pos = md.get("pos")
            if pos is None:
                mem = h.get("memory", "")
                pos = texts.index(mem) if mem in texts else None
            if pos is not None:
                out.append(int(pos))
        return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=", 1)[0][2:]: a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    path = args[0] if args else "tmp/longmemeval_s.json"
    out = args[1] if len(args) > 1 else "eval_mem_compare/results_mem0.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data
    start = int(opts.get("start", 0))
    end = int(opts.get("end", len(items)))
    shard = items[start:end]
    print(f"mem0 shard items[{start}:{end}] = {len(shard)} items", file=sys.stderr, flush=True)

    import shutil
    qpath = f"/data/asys-mem/eval_mem_compare/qdrant_shards/q{start}"
    shutil.rmtree(qpath, ignore_errors=True)
    os.makedirs(qpath, exist_ok=True)
    r = Mem0Retriever(qdrant_path=qpath)
    r.prepare(shard, base_idx=start)

    raw_by_k = {}
    for k in K_GRID:
        t0 = time.time()
        raw = tally_raw(shard, r, k, base_idx=start)
        raw_by_k[str(k)] = raw
        fin = finalize(raw, k)
        print(f"[shard {start}] k={k:2d}  recall@k={fin['recall_at_k']:.4f}  "
              f"scored={raw['scored']}  ({time.time()-t0:.1f}s)", file=sys.stderr, flush=True)

    outp = out.replace(".json", f"_shard{start}_{end}.json")
    Path(outp).write_text(json.dumps({"start": start, "end": end, "raw_by_k": raw_by_k}, indent=2))
    print(f"wrote {outp}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
