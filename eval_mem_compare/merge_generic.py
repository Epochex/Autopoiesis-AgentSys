"""Merge any named raw shard files ({name,start,end,raw_by_k}) into a results dict."""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from harness import finalize

K_GRID = [1, 3, 5, 10]


def main() -> int:
    pattern = sys.argv[1]
    out = sys.argv[2]
    shards = sorted(glob.glob(pattern))
    if not shards:
        print(f"no shard files match {pattern}", file=sys.stderr)
        return 1
    name = None
    merged = {str(k): {"recall_hits": 0, "answer_hits": 0, "scored": 0, "n": 0, "by_type": {}} for k in K_GRID}
    covered = []
    for sf in shards:
        d = json.loads(Path(sf).read_text())
        name = name or d.get("name", "system")
        covered.append((d["start"], d["end"]))
        for k in K_GRID:
            raw = d["raw_by_k"][str(k)]
            m = merged[str(k)]
            for key in ("recall_hits", "answer_hits", "scored", "n"):
                m[key] += raw[key]
            for t, lst in raw["by_type"].items():
                m["by_type"].setdefault(t, []).extend(lst)
    result = {name: {str(k): finalize(merged[str(k)], k) for k in K_GRID}}
    Path(out).write_text(json.dumps(result, indent=2))
    covered.sort()
    print(f"merged {len(shards)} shards for '{name}', covered {covered}", file=sys.stderr)
    print(f"n@5={merged['5']['n']} scored@5={merged['5']['scored']}", file=sys.stderr)
    for k in K_GRID:
        print(f"  {name} recall@{k} = {result[name][str(k)]['recall_at_k']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
