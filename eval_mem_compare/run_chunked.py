"""Fair full-text dense baseline: max-pool over 256-token chunks (same embedder).

The plain vector / Mem0 paths truncate each ~9750-char session to MiniLM's first
256 tokens, so they never see most of the session — yet they already beat the
tiered system. This variant removes that handicap: every session is split into
256-token chunks, all chunks are embedded, and a session's score is the MAX
cosine of any of its chunks to the query (late-interaction / ColBERT-style
max-pool — the answer usually lives in ONE chunk). This is the *strongest fair*
dense representation, so it is the honest ceiling for the dense/Mem0 family and
cannot be accused of strawmanning them.

Sharded like run_mem0 (items are independent); raw counts merged by merge_generic.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402

from harness import EMBED_MODEL, finalize, session_texts, tally_raw  # noqa: E402

K_GRID = [1, 3, 5, 10]
CHUNK_WORDS = 180        # ~256 MiniLM tokens


def chunk_words(text: str, n: int = CHUNK_WORDS) -> list[str]:
    w = text.split()
    if not w:
        return [""]
    return [" ".join(w[i:i + n]) for i in range(0, len(w), n)]


class ChunkedMaxPoolRetriever:
    name = "flat vector full-text (max-pool chunks)"

    def __init__(self):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(EMBED_MODEL)
        self.item_chunk_emb: dict[int, np.ndarray] = {}      # (n_chunks, dim)
        self.item_chunk_sess: dict[int, np.ndarray] = {}     # chunk -> local session idx
        self.item_qvec: dict[int, np.ndarray] = {}

    def prepare(self, items, base_idx=0):
        t0 = time.time()
        for j, item in enumerate(items):
            gidx = base_idx + j
            texts, _ = session_texts(item)
            chunks: list[str] = []
            owner: list[int] = []
            for si, t in enumerate(texts):
                for ch in chunk_words(t):
                    chunks.append(ch)
                    owner.append(si)
            emb = self.model.encode(chunks, normalize_embeddings=True, batch_size=256, show_progress_bar=False)
            self.item_chunk_emb[gidx] = np.asarray(emb, dtype=np.float32)
            self.item_chunk_sess[gidx] = np.asarray(owner, dtype=np.int32)
            self.item_qvec[gidx] = np.asarray(
                self.model.encode([str(item.get("question", ""))], normalize_embeddings=True)[0], dtype=np.float32)
            if (j + 1) % 10 == 0:
                print(f"  [chunk shard {base_idx}] {j+1} items ({time.time()-t0:.0f}s)", file=sys.stderr, flush=True)

    def retrieve(self, idx, texts, sids, question, k):
        ce = self.item_chunk_emb[idx]
        owner = self.item_chunk_sess[idx]
        q = self.item_qvec[idx]
        chunk_sims = ce @ q
        n_sess = len(texts)
        sess_score = np.full(n_sess, -1e9, dtype=np.float32)
        np.maximum.at(sess_score, owner, chunk_sims)         # max cosine per session
        order = sorted(range(n_sess), key=lambda i: (-float(sess_score[i]), i))
        return order[:k]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=", 1)[0][2:]: a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    path = args[0] if args else "tmp/longmemeval_s.json"
    out = args[1] if len(args) > 1 else "eval_mem_compare/results_chunked.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data
    start = int(opts.get("start", 0))
    end = int(opts.get("end", len(items)))
    shard = items[start:end]
    print(f"chunked shard items[{start}:{end}] = {len(shard)} items", file=sys.stderr, flush=True)

    r = ChunkedMaxPoolRetriever()
    r.prepare(shard, base_idx=start)
    raw_by_k = {}
    for k in K_GRID:
        raw = tally_raw(shard, r, k, base_idx=start)
        raw_by_k[str(k)] = raw
        print(f"[chunk shard {start}] k={k} recall@k={finalize(raw,k)['recall_at_k']:.4f}", file=sys.stderr, flush=True)
    outp = out.replace(".json", f"_shard{start}_{end}.json")
    Path(outp).write_text(json.dumps({"name": r.name, "start": start, "end": end, "raw_by_k": raw_by_k}, indent=2))
    print(f"wrote {outp}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
