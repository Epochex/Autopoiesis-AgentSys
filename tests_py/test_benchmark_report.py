"""Property tests for the benchmark-scenario report (``/api/rca/benchmark``).

These lock the honest guarantees of ``core.eval.benchmark_report``:

* LongMemEval-500 is a REAL local run (``live: true``); its systems include the
  self-authored tiered store and its recall values match the source json verbatim.
* ITBench is a PUBLISHED baseline only (``live: false``) with 3 groups and the real
  paper/repo/leaderboard urls.
* the coverage matrix carries exactly the 4 contract entries.
"""
from __future__ import annotations

import json

from core.eval import benchmark_report as br


def _raw_results() -> dict:
    merged: dict = {}
    for fp in (br._RESULTS_CORE, br._RESULTS_MEM0):
        merged.update(json.loads(fp.read_text()))
    return merged


def test_report_ok_and_top_level_shape() -> None:
    report = br.build_benchmark_report("zh")
    assert report["ok"] is True
    assert set(report) >= {"longmemeval", "itbench", "coverage"}


def test_longmemeval_live_and_has_self_system() -> None:
    lme = br.build_benchmark_report("en")["longmemeval"]
    assert lme["live"] is True
    assert lme["dataset"] == "LongMemEval-500"
    assert lme["n"] == 500
    assert lme["ks"] == [1, 3, 5, 10]
    assert len(lme["abilities"]) == 6

    selves = [s for s in lme["systems"] if s["self"]]
    assert len(selves) == 1
    assert selves[0]["id"] == "tiered"


def test_longmemeval_recall_matches_source_json() -> None:
    raw = _raw_results()
    lme = br.build_benchmark_report("zh")["longmemeval"]
    id_to_name = {
        "tiered": "tiered (this repo)",
        "mem0": "Mem0 (mem0ai, infer=False)",
        "reflexion": "Reflexion (reflective retrieval)",
        "flat_vector": "flat vector (same embedder)",
        "bm25": "BM25 (lexical floor)",
    }
    seen = set()
    for sys in lme["systems"]:
        name = id_to_name[sys["id"]]
        seen.add(sys["id"])
        for k in ("1", "3", "5", "10"):
            expected = round(raw[name][k]["recall_at_k"], 4)
            assert sys["recall"][k] == expected
        # ans_hit5 + per-ability at k=5 are read verbatim from k=5 cell.
        assert sys["ans_hit5"] == round(raw[name]["5"]["answer_string_hit"], 4)
        for ability, val in raw[name]["5"]["by_type"].items():
            assert sys["by_ability5"][ability] == round(val, 4)
    # all five real systems emitted
    assert seen == set(id_to_name)


def test_longmemeval_note_is_honest_about_bm25_floor() -> None:
    raw = _raw_results()
    # ground-truth honesty precondition: BM25 really does beat tiered at recall@5.
    assert raw["BM25 (lexical floor)"]["5"]["recall_at_k"] > raw["tiered (this repo)"]["5"]["recall_at_k"]
    note = br.build_benchmark_report("zh")["longmemeval"]["note"]
    assert "BM25" in note["zh"] and "BM25" in note["en"]


def test_itbench_published_baseline() -> None:
    it = br.build_benchmark_report("zh")["itbench"]
    assert it["live"] is False
    assert it["name"] == "ITBench"
    assert it["total_scenarios"] == 102
    assert it["paper_url"] == "https://arxiv.org/abs/2502.05352"
    assert it["repo_url"] == "https://github.com/itbench-hub/ITBench"
    assert it["leaderboard_url"] == "https://artificialanalysis.ai/evaluations/itbench-aa"

    groups = it["groups"]
    assert [g["id"] for g in groups] == ["sre", "ciso", "finops"]
    assert [g["sota_pct"] for g in groups] == [11.4, 25.2, 25.8]
    for g in groups:
        assert set(g["label"]) == {"zh", "en"}
        assert set(g["maps_to"]) == {"zh", "en"}


def test_coverage_matrix_has_four_entries() -> None:
    coverage = br.build_benchmark_report("en")["coverage"]
    assert len(coverage) == 4
    covered = {tuple(sorted(c["cap"].items())): c["covered"] for c in coverage}
    assert list(c["covered"] for c in coverage) == [True, True, True, False]
    # the self-evolution row is the honest "no public benchmark" one.
    novel = coverage[-1]
    assert novel["covered"] is False
    assert isinstance(novel["benchmark"], dict)
    assert set(novel["benchmark"]) == {"zh", "en"}
