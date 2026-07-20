"""Merge mem0 shard raw counts into results_mem0.json (identical to a single pass)."""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from harness import finalize

K_GRID = [1, 3, 5, 10]


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "eval_mem_compare/results_mem0_shard*.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "eval_mem_compare/results_mem0.json"
    shards = sorted(glob.glob(pattern))
    if not shards:
        print(f"no shard files match {pattern}", file=sys.stderr)
        return 1
    name = "Mem0 (mem0ai, infer=False)"
    merged = {str(k): {"recall_hits": 0, "answer_hits": 0, "scored": 0, "n": 0, "by_type": {}} for k in K_GRID}
    covered = []
    for sf in shards:
        d = json.loads(Path(sf).read_text())
        covered.append((d["start"], d["end"]))
        for k in K_GRID:
            raw = d["raw_by_k"][str(k)]
            m = merged[str(k)]
            m["recall_hits"] += raw["recall_hits"]
            m["answer_hits"] += raw["answer_hits"]
            m["scored"] += raw["scored"]
            m["n"] += raw["n"]
            for t, lst in raw["by_type"].items():
                m["by_type"].setdefault(t, []).extend(lst)
    result = {name: {str(k): finalize(merged[str(k)], k) for k in K_GRID}}
    Path(out).write_text(json.dumps(result, indent=2))
    covered.sort()
    print(f"merged {len(shards)} shards covering {covered}", file=sys.stderr)
    print(f"total n at k=5: {merged['5']['n']}, scored: {merged['5']['scored']}", file=sys.stderr)
    for k in K_GRID:
        print(f"  Mem0 recall@{k} = {result[name][str(k)]['recall_at_k']}", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
