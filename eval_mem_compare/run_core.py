"""Run the LLM-free systems (tiered / BM25 / flat-vector / Reflexion) on
LongMemEval-500 through the single shared metric, at k in {1,3,5,10}.

Mem0 is run separately (run_mem0.py) because it drives its own embedding +
vector-store pipeline; its numbers are merged in report.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from harness import BM25Retriever, ReflexionRetriever, TieredRetriever, VectorRetriever, score_system

K_GRID = [1, 3, 5, 10]


def load_items(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("items", data) if isinstance(data, dict) else data


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "tmp/longmemeval_s.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "eval_mem_compare/results_core.json"
    items = load_items(path)
    print(f"loaded {len(items)} items from {path}", file=sys.stderr)

    from embedder import Embedder
    t0 = time.time()
    embedder = Embedder()
    embedder.prepare(items)
    print(f"embedded all sessions+questions in {time.time()-t0:.1f}s", file=sys.stderr)

    systems = [
        TieredRetriever(),
        BM25Retriever(),
        VectorRetriever(embedder),
        ReflexionRetriever(embedder),
    ]
    for s in systems:
        if hasattr(s, "prepare"):
            s.prepare(items)

    results: dict[str, dict] = {}
    for s in systems:
        results[s.name] = {}
        for k in K_GRID:
            t0 = time.time()
            res = score_system(items, s, k)
            results[s.name][str(k)] = res
            print(f"{s.name:34s} k={k:2d}  recall@k={res['recall_at_k']:.4f}  "
                  f"ans_hit={res['answer_string_hit']:.4f}  ({time.time()-t0:.1f}s)", file=sys.stderr)

    Path(out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
