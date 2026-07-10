"""Deterministic topology-memory retrieval precision eval.

This is an LLM-free harness for the structured topology graph memory. It compares
logical entity/relation/path retrieval against a naive token-overlap stand-in for
embedding RAG. The default fixture is synthetic and labelled as such: it is meant
to expose the retrieval mechanism under controlled multi-hop topology distractors,
not to claim a real-network benchmark number.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from core.memory.logical_retrieval import logical_retrieve, naive_similarity_retrieve
from core.memory.topo_graph import TopoGraphMemory, TopoRecord


_DEFAULT_FIXTURE = Path("domains/network_rca/fixtures/topo_incidents.json")


def load_fixture(fixtures: str | Path | dict | list | None = None) -> dict:
    """Normalize a fixture path / dict / bare record-list into {records, queries}."""
    if fixtures is None:
        fixtures = _DEFAULT_FIXTURE
    if isinstance(fixtures, (str, Path)):
        return json.loads(Path(fixtures).read_text(encoding="utf-8"))
    if isinstance(fixtures, list):
        return {"records": fixtures, "queries": []}
    return fixtures


def run_retrieval_eval(fixtures: str | Path | dict | list | None = None) -> dict:
    """Score logical vs naive retrieval on every fixture query.

    Returns per-method aggregate precision@k / recall@k / false-retrieval plus the
    per-query rows behind them. Fully deterministic and LLM-free.
    """
    fixture = load_fixture(fixtures)
    records = fixture.get("records", [])
    queries = fixture.get("queries", [])
    graph = TopoGraphMemory(records)

    logical_rows: list[dict] = []
    naive_rows: list[dict] = []
    for item in queries:
        if "id" not in item or "query" not in item:
            raise ValueError(f"fixture query missing required 'id'/'query' keys: {item}")
        query = item["query"]
        k = int(item.get("k", 3))
        relevant = set(item.get("relevant_ids", []))
        logical_rows.append(_score_query(logical_retrieve(query, graph, k), relevant, k, item["id"]))
        naive_rows.append(_score_query(naive_similarity_retrieve(query, records, k), relevant, k, item["id"]))

    methods = {
        "logical": _aggregate(logical_rows),
        "naive": _aggregate(naive_rows),
    }
    return {
        "dataset_kind": fixture.get("dataset_kind", "unknown"),
        "n_queries": len(queries),
        "methods": methods,
        "per_query": {
            "logical": logical_rows,
            "naive": naive_rows,
        },
    }


def _score_query(retrieved: list[TopoRecord], relevant: set[str], k: int, query_id: str) -> dict:
    """Precision@k penalizes under-retrieval (hits/k); returning nothing scores
    0.0 false-retrieval — abstention is not hallucination."""
    ids = [rec.id for rec in retrieved]
    hits = [rid for rid in ids if rid in relevant]
    false = [rid for rid in ids if rid not in relevant]
    return {
        "query_id": query_id,
        "k": k,
        "retrieved_ids": ids,
        "relevant_ids": sorted(relevant),
        "hits": len(hits),
        "retrieved": len(ids),
        "precision_at_k": round(len(hits) / k, 4) if k else 0.0,
        "recall_at_k": round(len(hits) / len(relevant), 4) if relevant else 0.0,
        "false_retrieval": round(len(false) / max(1, len(ids)), 4),
    }


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"precision_at_k": 0.0, "recall_at_k": 0.0, "false_retrieval": 0.0}
    return {
        "precision_at_k": round(sum(row["precision_at_k"] for row in rows) / len(rows), 4),
        "recall_at_k": round(sum(row["recall_at_k"] for row in rows) / len(rows), 4),
        "false_retrieval": round(sum(row["false_retrieval"] for row in rows) / len(rows), 4),
    }


def _print_table(res: dict) -> None:
    print(f"dataset_kind: {res['dataset_kind']}")
    print("method | precision@k | recall@k | false-retrieval")
    print("--- | ---: | ---: | ---:")
    for name in ("logical", "naive"):
        row = res["methods"][name]
        print(
            f"{name} | {row['precision_at_k']:.4f} | "
            f"{row['recall_at_k']:.4f} | {row['false_retrieval']:.4f}"
        )
    logical = res["methods"]["logical"]
    naive = res["methods"]["naive"]
    print(
        "delta: "
        f"precision@k {logical['precision_at_k'] - naive['precision_at_k']:+.4f}, "
        f"recall@k {logical['recall_at_k'] - naive['recall_at_k']:+.4f}, "
        f"false-retrieval {logical['false_retrieval'] - naive['false_retrieval']:+.4f}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    fixture = argv[0] if argv else None
    res = run_retrieval_eval(fixture)
    _print_table(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
