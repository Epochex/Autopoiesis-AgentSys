"""Skill-retrieval eval on the REAL FortiGate held-out set — LLM-free, zero-dep.

The retrieval task the RCA agent actually faces: given an incident query in natural
language, surface the right read-only *probe skills* to run, out of the full skill
catalog (which includes a distractor skill that is never relevant). Ground truth is
the ``relevant_skills`` shipped with each real held-out case — no labels invented
here.

Corpus  : the 9 real skills in ``domains.network_rca.skills.real_skills``
          (name + operation + description + curated tags).
Queries : the 6 real held-out cases' natural-language ``query`` strings.
Truth   : each case's ``relevant_skills``.

Four pluggable retrievers are compared on identical inputs:
  * ``naive``      — bag-of-words overlap fraction (the existing straw-man baseline);
  * ``bm25``       — Okapi BM25 sparse lexical (:mod:`core.memory.bm25`);
  * ``structured`` — curated-tag overlap (structured metadata, no free text);
  * ``rrf``        — Reciprocal Rank Fusion of bm25 + structured.

Every method is deterministic. Nothing here downloads a model or calls an LLM.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from core.memory.bm25 import BM25Index, tokenize
from core.memory.rrf import rrf_fuse
from core.memory.query_expansion import make_transform
from domains.network_rca.skills.real_skills import REAL_SKILL_OPERATIONS

_HELDOUT = Path("domains/network_rca/fixtures/real/heldout_cases.json")
_K_VALUES = (1, 2, 3)
_HEADLINE_K = 3


def build_skill_corpus(doc_transform=None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (doc_tokens, tag_tokens) keyed by skill name, from the real catalog.

    ``doc_tokens`` is the full lexical document (name + operation + description +
    tags) used by naive/BM25; ``tag_tokens`` is only the curated structured tags,
    used by the structured retriever. ``doc_transform`` (e.g. a stemmer) is applied
    to both, symmetrically with the query transform, so all methods share a
    vocabulary — default identity keeps the pre-existing behaviour.
    """
    doc_transform = doc_transform or (lambda toks: toks)
    docs: dict[str, list[str]] = {}
    tags: dict[str, list[str]] = {}
    for name, (operation, tag_list) in REAL_SKILL_OPERATIONS.items():
        description = f"Readonly real-syslog RCA check for {operation}"
        text = " ".join([name.replace("_", " "), operation.replace("_", " "), description, " ".join(tag_list)])
        docs[name] = doc_transform(tokenize(text))
        tags[name] = doc_transform([t.lower() for t in tag_list])
    return docs, tags


# ── retrievers: each maps (query_text, k) -> ranked list of skill names ────────
def _naive_retriever(docs, q_transform) -> Callable[[str, int], list[str]]:
    doc_sets = {d: set(toks) for d, toks in docs.items()}

    def retrieve(query: str, k: int) -> list[str]:
        if k <= 0:
            return []
        qterms = set(q_transform(tokenize(query)))
        if not qterms:
            return []
        scored = []
        for name in sorted(doc_sets):
            overlap = qterms & doc_sets[name]
            if overlap:
                scored.append((len(overlap) / len(qterms), name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [n for _, n in scored[:k]]

    return retrieve


def _structured_retriever(tags, q_transform) -> Callable[[str, int], list[str]]:
    def retrieve(query: str, k: int) -> list[str]:
        if k <= 0:
            return []
        qterms = set(q_transform(tokenize(query)))
        scored = []
        for name in sorted(tags):
            hits = sum(1 for tag in tags[name] if tag in qterms)
            if hits:
                scored.append((hits, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [n for _, n in scored[:k]]

    return retrieve


def build_retrievers(mode: str = "base") -> dict[str, Callable[[str, int], list[str]]]:
    """Build the four retrievers under a query-expansion ``mode`` (base/stem/expand)."""
    q_transform, d_transform = make_transform(mode)
    docs, tags = build_skill_corpus(d_transform)
    bm25 = BM25Index(docs)
    naive = _naive_retriever(docs, q_transform)
    structured = _structured_retriever(tags, q_transform)

    def rrf(query: str, k: int) -> list[str]:
        pool = max(k, _HEADLINE_K)
        qt = q_transform(tokenize(query))
        bm25_rank = [d for d, _ in bm25.rank_with_scores(query, pool, query_tokens=qt)]
        return rrf_fuse([bm25_rank, structured(query, pool)], k)

    def bm25_retrieve(query: str, k: int) -> list[str]:
        return [d for d, _ in bm25.rank_with_scores(query, k, query_tokens=q_transform(tokenize(query)))]

    return {
        "naive": naive,
        "bm25": bm25_retrieve,
        "structured": structured,
        "rrf": rrf,
    }


def load_heldout(path: str | Path | None = None) -> list[dict]:
    cases = json.loads(Path(path or _HELDOUT).read_text(encoding="utf-8"))
    return [
        {
            "id": c["case"]["id"],
            "query": c["case"]["query"],
            "relevant": set(c["case"]["relevant_skills"]),
        }
        for c in cases
    ]


def _score(retrieved: list[str], relevant: set[str], k: int) -> dict:
    hits = [r for r in retrieved if r in relevant]
    false = [r for r in retrieved if r not in relevant]
    return {
        "recall_at_k": len(hits) / len(relevant) if relevant else 0.0,
        "precision_at_k": len(hits) / k if k else 0.0,
        "false_retrieval": len(false) / max(1, len(retrieved)),
    }


def run_skill_retrieval_eval(path: str | Path | None = None, mode: str = "base") -> dict:
    """Score every retriever at each k on the real held-out cases (deterministic).

    ``mode`` selects the query-expansion transform: ``base`` (identity), ``stem``
    (symmetric stemming), or ``expand`` (query stem+synonyms).
    """
    cases = load_heldout(path)
    retrievers = build_retrievers(mode)
    out: dict = {"dataset_kind": "real-fortigate-heldout", "mode": mode, "n_queries": len(cases),
                 "n_skills": len(REAL_SKILL_OPERATIONS), "methods": {}}
    for method, retrieve in retrievers.items():
        by_k: dict[int, dict] = {}
        for k in _K_VALUES:
            rows = [_score(retrieve(c["query"], k), c["relevant"], k) for c in cases]
            by_k[k] = {
                metric: round(sum(r[metric] for r in rows) / len(rows), 4)
                for metric in ("recall_at_k", "precision_at_k", "false_retrieval")
            }
        out["methods"][method] = by_k
    return out


def _print(res: dict) -> None:
    print(f"dataset: {res['dataset_kind']}  ({res['n_queries']} real held-out queries, "
          f"{res['n_skills']}-skill catalog, LLM-free)")
    print(f"\nrecall@k / false-retrieval@k, averaged over {res['n_queries']} cases:")
    header = "method".ljust(12) + "".join(f"R@{k}".rjust(9) for k in _K_VALUES) + "   " + \
             "".join(f"FR@{k}".rjust(9) for k in _K_VALUES)
    print(header)
    print("-" * len(header))
    for method in ("naive", "bm25", "structured", "rrf"):
        by_k = res["methods"][method]
        row = method.ljust(12)
        row += "".join(f"{by_k[k]['recall_at_k']:.3f}".rjust(9) for k in _K_VALUES)
        row += "   " + "".join(f"{by_k[k]['false_retrieval']:.3f}".rjust(9) for k in _K_VALUES)
        print(row)
    naive_r = res["methods"]["naive"][_HEADLINE_K]["recall_at_k"]
    rrf_r = res["methods"]["rrf"][_HEADLINE_K]["recall_at_k"]
    bm25_r = res["methods"]["bm25"][_HEADLINE_K]["recall_at_k"]
    print(f"\nheadline (k={_HEADLINE_K}): recall  naive {naive_r:.3f} -> bm25 {bm25_r:.3f} "
          f"-> rrf {rrf_r:.3f}   (delta rrf-naive {rrf_r - naive_r:+.3f})")


def _print_modes(path: str | Path | None = None) -> None:
    """Headline recall@3 for base vs stem vs expand — the query-expansion effect."""
    print("query-expansion effect on real held-out recall@3 (avg over 6 cases):")
    for mode in ("base", "stem", "expand"):
        res = run_skill_retrieval_eval(path, mode)
        r = {m: res["methods"][m][_HEADLINE_K]["recall_at_k"] for m in ("naive", "bm25", "structured", "rrf")}
        note = {"base": "no expansion", "stem": "symmetric stemming (general)",
                "expand": "stem + netops synonyms (domain lexicon)"}[mode]
        print(f"  {mode:<7} naive {r['naive']:.3f}  bm25 {r['bm25']:.3f}  "
              f"structured {r['structured']:.3f}  rrf {r['rrf']:.3f}   ({note})")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else None
    _print(run_skill_retrieval_eval(path))
    print()
    _print_modes(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
