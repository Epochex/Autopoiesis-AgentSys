"""Merge results_core.json + results_mem0.json into the comparison table.

Prints a Markdown report: recall@k for k in {1,3,5,10} for every system, the
answer-string-hit at k=5, the per-ability breakdown at k=5, and tiered-vs-baseline
deltas. No numbers are computed here — this only formats what the runners scored.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

K_GRID = [1, 3, 5, 10]
# Row order: system under test first, then baselines.
ORDER = [
    "tiered (this repo)",
    "Mem0 (mem0ai, infer=False)",
    "Reflexion (reflective retrieval)",
    "flat vector (same embedder)",
    "BM25 (lexical floor)",
]


def load(*paths: str) -> dict:
    merged: dict = {}
    for p in paths:
        fp = Path(p)
        if fp.exists():
            merged.update(json.loads(fp.read_text()))
    return merged


def main() -> int:
    core = sys.argv[1] if len(sys.argv) > 1 else "eval_mem_compare/results_core.json"
    mem0 = sys.argv[2] if len(sys.argv) > 2 else "eval_mem_compare/results_mem0.json"
    r = load(core, mem0)
    names = [n for n in ORDER if n in r] + [n for n in r if n not in ORDER]

    print("## LongMemEval-500 — recall@k (single shared metric, LLM-free)\n")
    header = "| system | " + " | ".join(f"recall@{k}" for k in K_GRID) + " | ans-str-hit@5 |"
    print(header)
    print("|" + "---|" * (len(K_GRID) + 2))
    for n in names:
        row = [n]
        for k in K_GRID:
            cell = r[n].get(str(k), {})
            row.append(f"{cell.get('recall_at_k', float('nan')):.4f}" if cell else "—")
        h5 = r[n].get("5", {}).get("answer_string_hit")
        row.append(f"{h5:.4f}" if h5 is not None else "—")
        print("| " + " | ".join(row) + " |")

    # tiered vs baselines at each k
    base = "tiered (this repo)"
    if base in r:
        print(f"\n## Δ recall@k: tiered − baseline (positive = tiered wins)\n")
        print("| system | " + " | ".join(f"Δ@{k}" for k in K_GRID) + " |")
        print("|" + "---|" * (len(K_GRID) + 1))
        for n in names:
            if n == base:
                continue
            row = [n]
            for k in K_GRID:
                t = r[base].get(str(k), {}).get("recall_at_k")
                b = r[n].get(str(k), {}).get("recall_at_k")
                row.append(f"{t-b:+.4f}" if (t is not None and b is not None) else "—")
            print("| " + " | ".join(row) + " |")

    # per-ability at k=5
    print(f"\n## Per-ability recall@5\n")
    types: list[str] = []
    for n in names:
        types += list(r[n].get("5", {}).get("by_type", {}))
    types = sorted(set(types))
    print("| system | " + " | ".join(types) + " |")
    print("|" + "---|" * (len(types) + 1))
    for n in names:
        bt = r[n].get("5", {}).get("by_type", {})
        row = [n] + [f"{bt.get(t, float('nan')):.3f}" if t in bt else "—" for t in types]
        print("| " + " | ".join(row) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
