"""Fast single-process subset sanity check (isolates the truncation effect).
Single process => no torch oversubscription. NOT the headline number.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
from harness import TieredRetriever, BM25Retriever, VectorRetriever, finalize, tally_raw
from run_chunked import ChunkedMaxPoolRetriever

def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    data = json.loads(Path("tmp/longmemeval_s.json").read_text())
    items = (data.get("items", data) if isinstance(data, dict) else data)[:n]
    print(f"subset check, first {len(items)} items", file=sys.stderr, flush=True)
    from embedder import Embedder
    emb = Embedder(); emb.prepare(items)
    systems = [TieredRetriever(), BM25Retriever(), VectorRetriever(emb), ChunkedMaxPoolRetriever()]
    for s in systems:
        if hasattr(s, "prepare"): s.prepare(items)
    print(f"\n{'system':44s}  r@1    r@3    r@5    r@10")
    for s in systems:
        cells = [finalize(tally_raw(items, s, k), k)["recall_at_k"] for k in (1,3,5,10)]
        print(f"{s.name:44s}  " + "  ".join(f"{c:.3f}" for c in cells), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
