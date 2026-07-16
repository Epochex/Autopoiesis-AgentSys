"""Okapi BM25 sparse lexical retrieval — pure Python, zero dependencies.

This is the honest sparse-retrieval baseline for skill / evidence selection: a
proper IDF-weighted term-frequency model, not the bag-of-words overlap stand-in
in ``logical_retrieval.naive_similarity_retrieve``. It stays LLM-free and fully
deterministic (ties break on document id), so every number it produces is
reproducible anywhere with no model download.

Reference: Robertson & Zaragoza, 2009, "The Probabilistic Relevance Framework:
BM25 and Beyond." Defaults k1=1.5, b=0.75 are the standard Okapi settings.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "with", "at", "by", "from", "into", "over", "under", "this", "that", "which", "what", "why",
    "how", "shows", "show", "appear", "appears", "such", "as", "its",
}


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords and 1-char tokens dropped.

    Shared by every retriever in the skill-retrieval eval so all methods see the
    identical query/document vocabulary — the comparison is apples-to-apples.
    """
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP]


class BM25Index:
    """A fitted Okapi BM25 index over a fixed document set.

    Documents are ``(doc_id, tokens)`` pairs. ``rank`` returns doc ids ordered by
    descending BM25 score; documents with zero score (no query-term overlap) are
    omitted rather than returned at random, so abstention stays possible.
    """

    def __init__(self, documents: dict[str, list[str]], *, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids: list[str] = sorted(documents)
        self.doc_tokens: dict[str, list[str]] = {d: list(documents[d]) for d in self.doc_ids}
        self.doc_freqs: dict[str, Counter] = {d: Counter(t) for d, t in self.doc_tokens.items()}
        self.doc_len: dict[str, int] = {d: len(t) for d, t in self.doc_tokens.items()}
        n = len(self.doc_ids)
        self.avgdl: float = (sum(self.doc_len.values()) / n) if n else 0.0
        # document frequency per term, then smoothed IDF (never negative).
        df: Counter = Counter()
        for freqs in self.doc_freqs.values():
            df.update(freqs.keys())
        self.idf: dict[str, float] = {
            term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()
        }

    def score(self, query_tokens: list[str], doc_id: str) -> float:
        freqs = self.doc_freqs[doc_id]
        dl = self.doc_len[doc_id]
        denom_len = self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 0.0))
        total = 0.0
        for term in query_tokens:
            f = freqs.get(term, 0)
            if not f:
                continue
            total += self.idf.get(term, 0.0) * (f * (self.k1 + 1)) / (f + denom_len)
        return total

    def rank(self, query: str, k: int) -> list[str]:
        """Top-``k`` doc ids by BM25 score; zero-score docs dropped, ties on id."""
        return [doc_id for doc_id, _ in self.rank_with_scores(query, k)]

    def rank_with_scores(self, query: str, k: int, *, query_tokens: list[str] | None = None) -> list[tuple[str, float]]:
        """Top-``k`` ``(doc_id, score)`` pairs; zero-score docs dropped, ties on id.

        ``query_tokens`` lets a caller pass pre-normalised/expanded tokens (see
        :mod:`core.memory.query_expansion`) instead of re-tokenising the raw string.
        """
        if k <= 0:
            return []
        qtokens = query_tokens if query_tokens is not None else tokenize(query)
        scored = [(self.score(qtokens, d), d) for d in self.doc_ids]
        scored = [(s, d) for s, d in scored if s > 0.0]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(d, round(s, 6)) for s, d in scored[:k]]
