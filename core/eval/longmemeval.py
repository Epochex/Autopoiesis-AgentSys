"""LongMemEval — external long-term-memory benchmark, used here as the *external
conformance anchor* for the memory layer.

LongMemEval (Wu et al., ICLR 2025 — arXiv:2410.10813) stresses five abilities over
long, multi-session chat histories saturated with distractors: information
extraction, multi-session reasoning, temporal reasoning, knowledge updates, and
abstention. It is the authoritative memory benchmark this project targets.

This harness maps a LongMemEval item onto our ``TieredMemoryStore`` and measures the
metric our system is actually responsible for: out of every distractor session, does
tiered retrieval surface the answer-bearing session (recall@k), and is the answer
string present in what we retrieved (answer_string_hit). It is **LLM-free by design**
— it scores the memory/retrieval layer, not a generator — so the numbers are
reproducible on any machine with no API key.

Honesty contract
----------------
* The headline evidence for this project is the REAL R230 FortiGate held-out stream
  (see docs/BENCHMARKS.md): −75% probes at 100% accuracy, ablation 100%→17%.
* LongMemEval is the EXTERNAL anchor. Run it yourself on the real dataset:
      python -m core.eval.longmemeval /path/to/longmemeval_s.json
* A tiny synthetic fixture (tests_py/fixtures/longmemeval_synthetic.json) ships only
  to prove the harness runs — its scores are NOT LongMemEval results and are labelled
  as synthetic wherever they appear.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from core.memory.store import MemoryRecord, TieredMemoryStore

_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "with", "at", "by", "it", "this", "that", "you", "we", "they", "he", "she", "do", "did",
    "does", "my", "your", "what", "when", "which", "how", "who", "have", "has", "had", "been",
}


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP]


def load_longmemeval(path: str | Path) -> list[dict]:
    """Load a LongMemEval json (``longmemeval_s`` / ``_m`` / ``_oracle``), or raise
    with download instructions if it is not present."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"LongMemEval dataset not found at {p}. Download longmemeval_s.json from "
            "https://github.com/xiaowu0162/LongMemEval (or HuggingFace 'xiaowu0162/longmemeval') "
            "and pass its path."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("items", data) if isinstance(data, dict) else data


def _ingest(item: dict) -> tuple[TieredMemoryStore, dict[str, str]]:
    """One fresh store per item; every haystack session becomes one episodic memory,
    so retrieval must pick the answer session out of all the distractors."""
    mem = TieredMemoryStore()
    sessions = item.get("haystack_sessions", [])
    sids = item.get("haystack_session_ids") or [f"s{i}" for i in range(len(sessions))]
    if len(sids) != len(sessions):
        raise ValueError(
            f"item {item.get('question_id', '?')}: {len(sids)} haystack_session_ids "
            f"for {len(sessions)} haystack_sessions — corrupt dataset item"
        )
    rec_to_sid: dict[str, str] = {}
    for sid, turns in zip(sids, sessions):
        if isinstance(turns, dict):
            turns = turns.get("turns", [])
        text = " ".join(str(t.get("content", "")) for t in turns if isinstance(t, dict))
        mid = f"lme-{sid}"
        mem.add(MemoryRecord(memory_id=mid, tier="episodic", text=text, tags=_terms(text)[:48]))
        rec_to_sid[mid] = sid
    return mem, rec_to_sid


def run_longmemeval(items: list[dict], *, k: int = 5) -> dict:
    """Score memory retrieval on LongMemEval items. Returns recall@k of the
    answer-bearing session(s), an answer-string-hit rate, and a per-ability breakdown."""
    by_type: dict[str, list[int]] = {}
    recall_hits = 0
    answer_hits = 0
    scored = 0
    n = 0
    for item in items:
        answer_sids = set(item.get("answer_session_ids") or [])
        qtype = str(item.get("question_type", "unknown"))
        mem, rec_to_sid = _ingest(item)
        got = mem.retrieve(_terms(str(item.get("question", ""))), [], limit_per_tier=k)
        retrieved = got.get("episodic", [])
        retrieved_sids = {rec_to_sid.get(r.memory_id) for r in retrieved}
        n += 1
        answer = str(item.get("answer", "")).strip().lower()
        if answer and any(answer in r.text.lower() for r in retrieved):
            answer_hits += 1
        if not answer_sids:          # abstention items have no answer session — skip recall
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


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m core.eval.longmemeval <longmemeval_s.json> [k]", file=sys.stderr)
        return 2
    path = argv[0]
    try:
        k = int(argv[1]) if len(argv) > 1 else 5
    except ValueError:
        print(f"k must be an integer, got {argv[1]!r}", file=sys.stderr)
        return 2
    items = load_longmemeval(path)
    res = run_longmemeval(items, k=k)
    synthetic = "synthetic" in Path(path).name.lower()
    if synthetic:
        print("NOTE: synthetic fixture — NOT a LongMemEval result.", file=sys.stderr)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
