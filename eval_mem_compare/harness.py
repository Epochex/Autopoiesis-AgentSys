"""Fair, single-metric comparison harness for memory-retrieval systems on LongMemEval.

Every system is scored through ONE scoring loop (``score_system``) that is a
byte-faithful re-implementation of ``core.eval.longmemeval.run_longmemeval``:

  * one document per haystack session, text built identically to the repo's
    ``_ingest`` ( `" ".join(turn["content"])` );
  * retrieve top-k session positions;
  * recall@k = does the retrieved top-k intersect ``answer_session_ids``;
  * answer_string_hit = is the answer substring present in retrieved text.

A retriever is any object with ``.name`` and
``retrieve(item_idx, texts, sids, question, k) -> list[int]`` (0-based session
positions, best first). Global precompute (embeddings, BM25) happens in
``prepare(items)``. This guarantees no system gets an input another lacks, and
that the ONLY thing that varies across rows of the results table is the ranking
function — not the data, the metric, the budget, or the tokenisation.

Fairness anchors
----------------
* Same 500 items, same session documents, same recall@k, same k grid.
* Every embedding-based system uses the SAME model (``EMBED_MODEL``).
* The tiered system is the repo's real ``TieredMemoryStore.retrieve`` — no
  reimplementation, no relabelling; validated to reproduce the repo's 0.906.
"""
from __future__ import annotations

import re
from typing import Callable

# The one shared embedder for every system that uses embeddings.
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# --- shared tokenisation (mirrors core.eval.longmemeval._terms) -------------
_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "with", "at", "by", "it", "this", "that", "you", "we", "they", "he", "she", "do", "did",
    "does", "my", "your", "what", "when", "which", "how", "who", "have", "has", "had", "been",
}


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP]


def session_texts(item: dict) -> tuple[list[str], list[str]]:
    """Exactly the document each system indexes — identical to the repo's _ingest."""
    sessions = item.get("haystack_sessions", [])
    sids = item.get("haystack_session_ids") or [f"s{i}" for i in range(len(sessions))]
    if len(sids) != len(sessions):
        raise ValueError(f"item {item.get('question_id','?')}: sid/session length mismatch")
    texts: list[str] = []
    for turns in sessions:
        if isinstance(turns, dict):
            turns = turns.get("turns", [])
        texts.append(" ".join(str(t.get("content", "")) for t in turns if isinstance(t, dict)))
    return texts, sids


def tally_raw(items: list[dict], retriever, k: int, *, base_idx: int = 0) -> dict:
    """Raw, mergeable counts for the same metric — so a run can be sharded across
    processes and summed to the identical recall@k as a single 500-item pass.

    ``base_idx`` offsets the item index passed to the retriever, so a shard of
    items[S:E] still addresses the retriever's global namespace (e.g. Mem0 user_id).
    """
    by_type: dict[str, list[int]] = {}
    recall_hits = answer_hits = scored = n = 0
    for j, item in enumerate(items):
        idx = base_idx + j
        answer_sids = set(item.get("answer_session_ids") or [])
        qtype = str(item.get("question_type", "unknown"))
        texts, sids = session_texts(item)
        topk = retriever.retrieve(idx, texts, sids, str(item.get("question", "")), k)
        retrieved_sids = {sids[i] for i in topk}
        n += 1
        answer = str(item.get("answer", "")).strip().lower()
        if answer and any(answer in texts[i].lower() for i in topk):
            answer_hits += 1
        if not answer_sids:
            continue
        hit = int(bool(answer_sids & retrieved_sids))
        recall_hits += hit
        scored += 1
        by_type.setdefault(qtype, []).append(hit)
    return {"recall_hits": recall_hits, "answer_hits": answer_hits, "scored": scored, "n": n, "by_type": by_type}


def finalize(raw: dict, k: int) -> dict:
    """Turn merged raw counts into the same dict shape score_system returns."""
    return {
        "n": raw["n"], "k": k, "scored": raw["scored"],
        "recall_at_k": round(raw["recall_hits"] / raw["scored"], 4) if raw["scored"] else 0.0,
        "answer_string_hit": round(raw["answer_hits"] / raw["n"], 4) if raw["n"] else 0.0,
        "by_type": {t: round(sum(v) / len(v), 4) for t, v in sorted(raw["by_type"].items())},
    }


def score_system(items: list[dict], retriever, k: int) -> dict:
    """The single shared metric. Byte-faithful to run_longmemeval, retriever swapped."""
    by_type: dict[str, list[int]] = {}
    recall_hits = answer_hits = scored = n = 0
    for idx, item in enumerate(items):
        answer_sids = set(item.get("answer_session_ids") or [])
        qtype = str(item.get("question_type", "unknown"))
        texts, sids = session_texts(item)
        question = str(item.get("question", ""))
        topk = retriever.retrieve(idx, texts, sids, question, k)
        retrieved_sids = {sids[i] for i in topk}
        n += 1
        answer = str(item.get("answer", "")).strip().lower()
        if answer and any(answer in texts[i].lower() for i in topk):
            answer_hits += 1
        if not answer_sids:                      # abstention — no recall to score
            continue
        hit = int(bool(answer_sids & retrieved_sids))
        recall_hits += hit
        scored += 1
        by_type.setdefault(qtype, []).append(hit)
    return {
        "n": n,
        "k": k,
        "scored": scored,
        "recall_at_k": round(recall_hits / scored, 4) if scored else 0.0,
        "answer_string_hit": round(answer_hits / n, 4) if n else 0.0,
        "by_type": {t: round(sum(v) / len(v), 4) for t, v in sorted(by_type.items())},
    }


# ======================================================================
#  Retrievers
# ======================================================================

class TieredRetriever:
    """THE project's system under test: core.memory.store.TieredMemoryStore.

    Built exactly as core.eval.longmemeval._ingest builds it (episodic records,
    tags = first 48 content terms), retrieved exactly as the harness retrieves
    (query = _terms(question), no asset ids, limit_per_tier=k). This is the real
    tiered retriever, not a stand-in — validated to reproduce recall@5 = 0.906.
    """

    name = "tiered (this repo)"

    def __init__(self):
        from core.memory.store import MemoryRecord, TieredMemoryStore  # noqa: F401
        self._MemoryRecord = MemoryRecord
        self._Store = TieredMemoryStore

    def retrieve(self, idx, texts, sids, question, k):
        mem = self._Store()
        pos_of_mid: dict[str, int] = {}
        for i, text in enumerate(texts):
            mid = f"lme-{i}-{sids[i]}"
            mem.add(self._MemoryRecord(memory_id=mid, tier="episodic", text=text, tags=terms(text)[:48]))
            pos_of_mid[mid] = i
        got = mem.retrieve(terms(question), [], limit_per_tier=k)
        return [pos_of_mid[r.memory_id] for r in got.get("episodic", [])]


class BM25Retriever:
    """Classic lexical floor: Okapi BM25 over the same session documents."""

    name = "BM25 (lexical floor)"

    def __init__(self):
        self._corpora: list = []

    def prepare(self, items):
        from rank_bm25 import BM25Okapi
        self._BM25Okapi = BM25Okapi
        for item in items:
            texts, _ = session_texts(item)
            self._corpora.append(BM25Okapi([terms(t) for t in texts]))

    def retrieve(self, idx, texts, sids, question, k):
        scores = self._corpora[idx].get_scores(terms(question))
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        return order[:k]


class VectorRetriever:
    """Semantic floor: flat vector store, cosine top-k over EMBED_MODEL embeddings.

    All embeddings are L2-normalised, so cosine == dot product. Ties broken by
    session position (stable), matching the repo's insertion-order tie-break.
    """

    name = "flat vector (same embedder)"

    def __init__(self, embedder):
        self._e = embedder            # shared Embedder instance (see runner)

    def prepare(self, items):
        self._doc = self._e.doc_matrices
        self._q = self._e.q_vectors

    def retrieve(self, idx, texts, sids, question, k):
        sims = self._doc[idx] @ self._q[idx]
        order = sorted(range(len(sims)), key=lambda i: (-float(sims[i]), i))
        return order[:k]


class ReflexionRetriever:
    """Reflexion-style reflective retrieval (Shinn et al., NeurIPS 2023,
    arXiv:2303.11366), adapted LLM-free.

    Faithful mechanism reproduced: attempt -> self-reflect on the attempt ->
    store the reflection -> retry using the reflection. Here:
      1. attempt: vector-retrieve top-m sessions for the question (same embedder);
      2. reflect: build a verbal reflection from the salient content of that
         attempt (the LLM-free stand-in for Reflexion's LLM-written reflection),
         drawn ONLY from retrieved document text — never from labels/answers;
      3. store + retry: append the reflection to the query, re-embed, re-rank,
         return top-k.

    Two reductions are stated honestly in the report: (a) LongMemEval is
    single-shot with a fresh store per item, so Reflexion's *cross-trial*
    feedback buffer — its core learning signal — has no purchase; (b) the
    reflection is extractive, not LLM-generated. This is the closest faithful
    single-item analog (attempt->reflect->retry via reflection-augmented query).
    """

    name = "Reflexion (reflective retrieval)"

    def __init__(self, embedder, m: int = 5, n_reflect_terms: int = 12):
        self._e = embedder
        self.m = m
        self.n_reflect_terms = n_reflect_terms

    def prepare(self, items):
        self._doc = self._e.doc_matrices
        self._q = self._e.q_vectors

    def retrieve(self, idx, texts, sids, question, k):
        doc = self._doc[idx]
        q = self._q[idx]
        sims = doc @ q
        order = sorted(range(len(sims)), key=lambda i: (-float(sims[i]), i))
        top_m = order[: self.m]
        # reflect: most salient content terms across the first-attempt sessions
        from collections import Counter
        c: Counter = Counter()
        for i in top_m:
            c.update(set(terms(texts[i])))          # set(): term presence, not raw frequency
        for w in terms(question):
            c.pop(w, None)                           # reflection adds NEW salient context
        reflection = " ".join(w for w, _ in c.most_common(self.n_reflect_terms))
        aug_query = (question + " " + reflection).strip()
        q2 = self._e.encode_query(aug_query)         # re-embed the reflection-augmented query
        sims2 = doc @ q2
        order2 = sorted(range(len(sims2)), key=lambda i: (-float(sims2[i]), i))
        return order2[:k]
