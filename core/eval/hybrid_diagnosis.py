"""Diagnose BM25+dense RRF on the fair, no-time IODA retrieval task.

This eval-only script reuses :mod:`core.eval.ioda_retrieval` for the corpus,
queries, labels, and BM25 route, and :mod:`core.eval.dense_retrieval` for the
query rendering, cached embeddings, and exact dense index.  It does not alter
any production retriever.

Top-10 experiments fuse each route's top 10, matching the depth used by the
existing IODA dense comparison when k=10.  Answer-rank statistics require a
complete ordering, so they extend dense to all corpus documents and BM25 to all
positive-score lexical candidates.  A truth document absent from BM25's
positive-score ranking is assigned the explicit worst-rank sentinel N+1.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Sequence

from core.eval import dense_retrieval as D
from core.eval import ioda_retrieval as R
from core.memory.rrf import rrf_fuse

_K = 10
_RRF_C = 60
_DENSE_WEIGHTS = (1.0, 0.75, 0.5, 0.25, 0.1, 0.0)


def weighted_rrf(
    bm25_ranking: Sequence[str],
    dense_ranking: Sequence[str],
    k: int,
    *,
    dense_weight: float,
    c: int = _RRF_C,
) -> list[str]:
    """Fuse BM25 and dense rankings, scaling only dense's RRF contribution."""
    if k <= 0:
        return []
    if dense_weight < 0.0:
        raise ValueError("dense_weight must be non-negative")

    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(bm25_ranking, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (c + rank)
    if dense_weight > 0.0:
        for rank, doc_id in enumerate(dense_ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + dense_weight / (c + rank)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [doc_id for doc_id, _ in fused[:k]]


def _recall_at_k(ranking: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranking[:k]) & relevant) / len(relevant) if relevant else 0.0


def _answer_rank(ranking: Sequence[str], relevant: set[str], not_found: int) -> int:
    """Return the 1-based rank of the first relevant document."""
    for rank, doc_id in enumerate(ranking, start=1):
        if doc_id in relevant:
            return rank
    return not_found


def _rank_summary(ranks: Sequence[int], *, not_found: int) -> dict:
    observed = [rank for rank in ranks if rank != not_found]
    return {
        "median": float(statistics.median(ranks)),
        "mean": sum(ranks) / len(ranks),
        "unranked": sum(rank == not_found for rank in ranks),
        "not_found_rank": not_found,
        "median_when_ranked": float(statistics.median(observed)) if observed else None,
        "mean_when_ranked": sum(observed) / len(observed) if observed else None,
    }


def _query_entities(query: R.Query) -> dict[str, list[str]]:
    return {
        "country": sorted(query.countries),
        "asn": sorted(query.asns),
    }


def _entity_relation(query: R.Query, record: dict) -> str:
    entity_type = str(record["entity_type"]).lower()
    entity = str(record["entity_id"]).lower()
    if entity_type == "country":
        if entity_type not in query.types:
            return "different_entity_type"
        return (
            "same_type_same_entity"
            if entity in query.countries
            else "same_type_different_entity"
        )
    if entity_type == "asn":
        if entity_type not in query.types:
            return "different_entity_type"
        return (
            "same_type_same_entity"
            if entity in query.asns
            else "same_type_different_entity"
        )
    return "different_entity_type"


def run_diagnosis(
    *,
    path: str | Path | None = None,
    model_name: str = D.DEFAULT_MODEL,
    max_events: int | None = None,
) -> dict:
    """Run all requested diagnostics on the fair IODA setup."""
    evidence = R.load_evidence(path)
    events = R.load_events(path)
    if max_events is not None:
        events = events[:max_events]
    queries = R.build_queries("base", path, max_events)
    if len(events) != len(queries):
        raise RuntimeError("event/query alignment failure")

    doc_ids = [R._doc_id(record["evidence_id"]) for record in evidence]
    doc_texts = [R._evidence_doc_text(record) for record in evidence]
    records_by_doc = dict(zip(doc_ids, evidence))
    relevant = R._relevant_sets(path)
    sparse = R.build_retrievers("base", path)
    bm25 = sparse["bm25"]
    structured_no_time = sparse["structured_no_time"]

    dense_index = D.DenseIndex.build(
        doc_ids,
        doc_texts,
        model_name=model_name,
        index_type="flat",
        cache_key=f"ioda_v2_{len(doc_ids)}",
    )
    query_texts = [D._ioda_query_text(event) for event in events]
    query_embeddings = D.embed(query_texts, model_name=model_name, is_query=True)
    # Use the existing dense wrapper at depth 10 for all headline experiments.
    # This preserves its exact boundary behavior when identical document texts
    # produce tied embeddings.
    dense_top_rows = dense_index.search_embeddings(query_embeddings, _K)
    # Search the complete corpus as one batch.  DenseIndex.search_embeddings would
    # eagerly materialize 7.1 million Python tuples for this run; retaining the two
    # compact numpy matrices and deterministically sorting one row at a time keeps
    # the full-rank diagnostic bounded in memory.
    import numpy as np

    dense_scores, dense_indices = dense_index.index.search(
        np.ascontiguousarray(query_embeddings, dtype="float32"), len(doc_ids)
    )

    recall_sums = {weight: 0.0 for weight in _DENSE_WEIGHTS}
    baseline_recall = {
        "bm25": 0.0,
        "dense-flat": 0.0,
        "rrf-fair-existing-3-route": 0.0,
        "rrf-bm25+dense": 0.0,
    }
    bm25_ranks: list[int] = []
    dense_ranks: list[int] = []
    rrf_ranks: list[int] = []
    dense_worse_count = 0
    bm25_win_hybrid_loss = 0
    hybrid_win_bm25_loss = 0
    bm25_higher_recall_queries = 0
    hybrid_higher_recall_queries = 0
    bm25_recall_advantage_sum = 0.0
    hybrid_recall_advantage_sum = 0.0
    dense_wrong_top1 = 0
    same_type_different_entity = 0
    same_type_same_entity = 0
    different_entity_type = 0
    samples: list[dict] = []
    not_found_rank = len(doc_ids) + 1

    for index, (event, query) in enumerate(zip(events, queries)):
        truth = relevant.get(query.event_id, set())
        bm25_full = bm25(query, len(doc_ids))
        dense_scored = [
            (doc_ids[doc_index], float(score))
            for score, doc_index in zip(dense_scores[index], dense_indices[index])
            if doc_index >= 0
        ]
        dense_scored.sort(key=lambda pair: (-pair[1], pair[0]))
        dense_full = [doc_id for doc_id, _ in dense_scored]

        bm25_top = bm25_full[:_K]
        dense_top = [doc_id for doc_id, _ in dense_top_rows[index]]
        fused_by_weight = {
            weight: weighted_rrf(
                bm25_top, dense_top, _K, dense_weight=weight
            )
            for weight in _DENSE_WEIGHTS
        }
        for weight, fused in fused_by_weight.items():
            recall_sums[weight] += _recall_at_k(fused, truth, _K)

        hybrid_top = fused_by_weight[1.0]
        bm25_hit = bool(set(bm25_top) & truth)
        hybrid_hit = bool(set(hybrid_top) & truth)
        bm25_win_hybrid_loss += int(bm25_hit and not hybrid_hit)
        hybrid_win_bm25_loss += int(hybrid_hit and not bm25_hit)

        bm25_query_recall = _recall_at_k(bm25_top, truth, _K)
        hybrid_query_recall = _recall_at_k(hybrid_top, truth, _K)
        if bm25_query_recall > hybrid_query_recall:
            bm25_higher_recall_queries += 1
            bm25_recall_advantage_sum += bm25_query_recall - hybrid_query_recall
        elif hybrid_query_recall > bm25_query_recall:
            hybrid_higher_recall_queries += 1
            hybrid_recall_advantage_sum += hybrid_query_recall - bm25_query_recall

        baseline_recall["bm25"] += bm25_query_recall
        baseline_recall["dense-flat"] += _recall_at_k(dense_top, truth, _K)
        existing_fair = rrf_fuse(
            [bm25_top, structured_no_time(query, _K), dense_top], _K
        )
        baseline_recall["rrf-fair-existing-3-route"] += _recall_at_k(
            existing_fair, truth, _K
        )
        baseline_recall["rrf-bm25+dense"] += hybrid_query_recall

        bm_rank = _answer_rank(bm25_full, truth, not_found_rank)
        dense_rank = _answer_rank(dense_full, truth, not_found_rank)
        rrf_full = weighted_rrf(
            bm25_full,
            dense_full,
            len(doc_ids),
            dense_weight=1.0,
        )
        rrf_rank = _answer_rank(rrf_full, truth, not_found_rank)
        bm25_ranks.append(bm_rank)
        dense_ranks.append(dense_rank)
        rrf_ranks.append(rrf_rank)
        dense_worse_count += int(dense_rank > bm_rank)

        # Failure examples use the exact top-1 surfaced by the existing dense
        # evaluator, including its depth-10 tie-boundary behavior.
        dense_top1 = dense_top[0]
        if dense_top1 not in truth:
            dense_wrong_top1 += 1
            predicted = records_by_doc[dense_top1]
            relation = _entity_relation(query, predicted)
            same_type_wrong_entity = relation == "same_type_different_entity"
            same_type_different_entity += int(same_type_wrong_entity)
            same_type_same_entity += int(relation == "same_type_same_entity")
            different_entity_type += int(relation == "different_entity_type")
            if len(samples) < 5:
                samples.append(
                    {
                        "event_id": event["event_id"],
                        "query_text": query_texts[index],
                        "query_entities": _query_entities(query),
                        "dense_top1": {
                            "doc_id": dense_top1,
                            "entity_type": str(predicted["entity_type"]).lower(),
                            "entity_id": str(predicted["entity_id"]),
                            "source": str(predicted["source"]),
                            "document_text": R._evidence_doc_text(predicted),
                            # Report-only label, never passed to either retriever.
                            "labeled_event_id": str(predicted["candidate_event_id"]),
                        },
                        "entity_relation": relation,
                        "same_entity_type_different_entity": same_type_wrong_entity,
                    }
                )

    n_queries = len(queries)
    if not n_queries:
        raise ValueError("diagnosis requires at least one query")
    sweep = {
        str(weight): recall_sums[weight] / n_queries
        for weight in _DENSE_WEIGHTS
    }
    sweep_values = [sweep[str(weight)] for weight in _DENSE_WEIGHTS]
    rises_toward_bm25 = all(
        later >= earlier
        for earlier, later in zip(sweep_values, sweep_values[1:])
    )
    if sweep["0.0"] != baseline_recall["bm25"] / n_queries:
        raise RuntimeError("zero dense weight did not reproduce BM25")
    if (
        same_type_different_entity
        + same_type_same_entity
        + different_entity_type
        != dense_wrong_top1
    ):
        raise RuntimeError("dense failure-mode categories do not sum to errors")

    return {
        "setting": {
            "dataset": "real-ioda-v2-three-source",
            "fair_text_only": True,
            "time_window_used": False,
            "model": model_name,
            "n_queries": n_queries,
            "n_corpus_docs": len(doc_ids),
            "k": _K,
            "rrf_c": _RRF_C,
            "top_k_route_depth": _K,
            "answer_rank_definition": "rank of first relevant document",
            "bm25_unranked_truth_rank": not_found_rank,
        },
        "baseline_recall_at_10": {
            method: total / n_queries
            for method, total in baseline_recall.items()
        },
        "dense_weight_sweep_recall_at_10": sweep,
        "recall_rises_monotonically_as_dense_weight_falls": rises_toward_bm25,
        "rank_of_truth": {
            "bm25": _rank_summary(bm25_ranks, not_found=not_found_rank),
            "dense-flat": _rank_summary(dense_ranks, not_found=not_found_rank),
            "rrf-bm25+dense": _rank_summary(rrf_ranks, not_found=not_found_rank),
            "dense_worse_than_bm25": {
                "count": dense_worse_count,
                "fraction": dense_worse_count / n_queries,
            },
        },
        "rrf_demotion": {
            "bm25_top10_hit_hybrid_top10_miss": bm25_win_hybrid_loss,
            "hybrid_top10_hit_bm25_top10_miss": hybrid_win_bm25_loss,
            "net_hybrid_query_wins": hybrid_win_bm25_loss - bm25_win_hybrid_loss,
            "bm25_higher_recall_queries": bm25_higher_recall_queries,
            "hybrid_higher_recall_queries": hybrid_higher_recall_queries,
            "equal_recall_queries": (
                n_queries
                - bm25_higher_recall_queries
                - hybrid_higher_recall_queries
            ),
            "summed_bm25_recall_advantage": bm25_recall_advantage_sum,
            "summed_hybrid_recall_advantage": hybrid_recall_advantage_sum,
        },
        "dense_failure_mode": {
            "wrong_top1_count": dense_wrong_top1,
            "same_entity_type_different_entity_count": same_type_different_entity,
            "same_entity_type_same_entity_count": same_type_same_entity,
            "different_entity_type_count": different_entity_type,
            "fraction_of_wrong_top1": (
                same_type_different_entity / dense_wrong_top1
                if dense_wrong_top1
                else 0.0
            ),
            "samples": samples,
        },
    }


def _print_report(result: dict) -> None:
    setting = result["setting"]
    print(
        f"FAIR IODA hybrid diagnosis: {setting['n_queries']} events / "
        f"{setting['n_corpus_docs']} documents, text-only, no time key"
    )
    print("\nbaseline macro recall@10")
    for method, value in result["baseline_recall_at_10"].items():
        print(f"  {method:<20} {value:.6f}")

    print("\ndense-weight sweep (BM25 contribution=1.0)")
    for weight, value in result["dense_weight_sweep_recall_at_10"].items():
        print(f"  dense w={weight:<4}  {value:.6f}")
    print(
        "  monotonic toward BM25: "
        + str(result["recall_rises_monotonically_as_dense_weight_falls"])
    )

    print("\nrank of first relevant document")
    ranks = result["rank_of_truth"]
    for method in ("bm25", "dense-flat", "rrf-bm25+dense"):
        row = ranks[method]
        print(
            f"  {method:<20} median={row['median']:.3f} "
            f"mean={row['mean']:.6f} unranked={row['unranked']}"
        )
    worse = ranks["dense_worse_than_bm25"]
    print(
        f"  dense worse than BM25: {worse['count']}/{setting['n_queries']} "
        f"({worse['fraction']:.6f})"
    )

    demotion = result["rrf_demotion"]
    print("\nRRF top-10 hit transitions")
    print(
        "  BM25 hit -> hybrid miss: "
        f"{demotion['bm25_top10_hit_hybrid_top10_miss']}"
    )
    print(
        "  BM25 miss -> hybrid hit: "
        f"{demotion['hybrid_top10_hit_bm25_top10_miss']}"
    )
    print(f"  net hybrid query wins: {demotion['net_hybrid_query_wins']}")
    print(
        "  higher per-query recall (BM25 / hybrid / equal): "
        f"{demotion['bm25_higher_recall_queries']} / "
        f"{demotion['hybrid_higher_recall_queries']} / "
        f"{demotion['equal_recall_queries']}"
    )
    print(
        "  summed recall advantage (BM25 / hybrid): "
        f"{demotion['summed_bm25_recall_advantage']:.6f} / "
        f"{demotion['summed_hybrid_recall_advantage']:.6f}"
    )

    failure = result["dense_failure_mode"]
    print("\ndense wrong-top-1 entity confusion")
    print(
        f"  same type, different entity: "
        f"{failure['same_entity_type_different_entity_count']}/"
        f"{failure['wrong_top1_count']} "
        f"({failure['fraction_of_wrong_top1']:.6f})"
    )
    print(
        "  same type, same entity: "
        f"{failure['same_entity_type_same_entity_count']}"
    )
    print(f"  different entity type: {failure['different_entity_type_count']}")
    for number, sample in enumerate(failure["samples"], start=1):
        predicted = sample["dense_top1"]
        print(
            f"  sample {number}: {sample['event_id']} | "
            f"query_entities={sample['query_entities']} | "
            f"top1={predicted['entity_type']}:{predicted['entity_id']} | "
            f"same-type/different-entity="
            f"{sample['same_entity_type_different_entity']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=None, help="IODA data directory")
    parser.add_argument("--model", default=D.DEFAULT_MODEL)
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    result = run_diagnosis(
        path=args.path,
        model_name=args.model,
        max_events=args.max_events,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
