from __future__ import annotations

import json

import pytest

from core.eval.memory_ablation import (
    DEFAULT_ARMS,
    POWER_STATEMENT,
    build_scenarios,
    hodges_lehmann_wilcoxon,
    mcnemar_mid_p,
    run_ablation,
)
from domains.network_rca.factory import load_seed_cases


@pytest.fixture(scope="module")
def report():
    # Two source cases keep the test quick while still producing both HOLD
    # variants at the requested one-third share.
    return run_ablation(load_seed_cases()[:2], arms=DEFAULT_ARMS, repeats=3, seed=71)


def test_report_keeps_power_boundary_and_complete_mcnemar_table(report):
    payload = report.to_dict()
    assert payload["power_statement"] == POWER_STATEMENT
    assert "10pp" in payload["power_statement"]
    comparisons = payload["metrics"]["M1"]["comparisons"]
    table = comparisons["A2_sham_memory"]["table_2x2"]
    assert table["cells"] == [
        [table["a_both_success"], table["b_memory_only"]],
        [table["c_baseline_only"], table["d_both_failure"]],
    ]
    assert sum(sum(row) for row in table["cells"]) == comparisons["A2_sham_memory"]["paired_n"]
    negative = payload["metrics"]["A2_vs_A1_negative_control"]
    assert negative["paired_n"] > 0
    assert "verified_repair_table_2x2" in negative
    assert negative["context_tokens_A2_minus_A1"]["hodges_lehmann"] is not None


def test_sham_memory_matches_memory_arm_context_within_ten_percent(report):
    """A2 matches the memory arm; A1 remains the context-volume control."""

    balance = report.design["sham_memory_balance"]
    assert balance["all_within_10_percent"]
    assert balance["all_retrieved_counts_equal"]
    assert balance["max_relative_difference"] <= 0.10

    fake_rows = report.raw_results["A2_sham_memory"]
    assert any(row.memory_injection["retrieved_count"] > 0 for row in fake_rows)
    for row in fake_rows:
        audit = row.memory_injection
        assert audit["retrieved_count"] == audit["target_retrieved_count"]
        assert audit["token_difference_ratio"] <= 0.10
        assert audit["max_semantic_overlap"] <= audit["overlap_threshold"]

    # A1 and A2 keep model-side controls equal. Realized length is allowed to
    # differ because that A1 to A2 delta is the pure context-volume estimand.
    by_name = {arm.name: arm for arm in DEFAULT_ARMS}
    assert by_name["A1_no_memory"].token_budget == by_name["A2_sham_memory"].token_budget
    assert by_name["A1_no_memory"].temperature == by_name["A2_sham_memory"].temperature


def test_hold_cases_are_derived_from_act_alerts_and_capability_gated(report):
    cases = load_seed_cases()[:2]
    scenarios = build_scenarios(cases, repeats=3, seed=71)
    holds = [scenario for scenario in scenarios if scenario.scenario_type != "ACT"]
    assert len(holds) / len(scenarios) == pytest.approx(1 / 3)
    assert {scenario.scenario_type for scenario in holds} == {
        "HOLD-healthy",
        "HOLD-outofscope",
    }
    for scenario in holds:
        source = next(case for case in cases if case.id == scenario.source_case_id)
        assert scenario.case.query == source.query
        assert scenario.case.query_terms == source.query_terms
        assert scenario.case.assets == source.assets

    m3 = report.metrics["M3"]
    assert m3["metrics_must_be_read_together"] == ["FIR", "COV", "SRP"]
    empty = m3["by_arm"]["A0_empty"]
    assert empty["capability_eligible_scenarios"] == 0
    assert empty["FIR"] is None


def test_m2_exposes_subset_size_and_small_sample_boundary(report):
    for comparison in report.metrics["M2"]["comparisons"].values():
        assert "both_success_subset_size_S" in comparison
        if comparison["both_success_subset_size_S"] < 10:
            assert comparison["interpretation"] == "descriptive only because |S| < 10"


def test_static_runbook_is_fixed_and_length_comparable(report):
    balance = report.design["static_runbook_balance"]
    assert balance["document_updated_during_run"] is False
    assert balance["relative_difference"] <= 0.20
    hashes = {
        row.memory_injection["document_sha256"]
        for row in report.raw_results["A3_static_runbook"]
    }
    assert len(hashes) == 1


def test_statistics_cover_discordance_and_zero_differences():
    assert mcnemar_mid_p(0, 0) == 1.0
    assert mcnemar_mid_p(3, 0) == pytest.approx(0.125)
    zeros = hodges_lehmann_wilcoxon([0, 0, 0])
    assert zeros["hodges_lehmann"] == 0.0
    assert zeros["wilcoxon_95_ci"] == [0.0, 0.0]


def test_report_is_json_serializable_and_default_path_never_calls_llm(report, monkeypatch):
    from core.llm.provider import OpenAICompatibleClient

    def forbidden(*args, **kwargs):
        raise AssertionError("offline ablation attempted an LLM call")

    monkeypatch.setattr(OpenAICompatibleClient, "complete_json", forbidden)
    rerun = run_ablation(load_seed_cases()[:1], arms=DEFAULT_ARMS, repeats=1, seed=9)
    assert rerun.design["llm_calls"] == 0
    json.dumps(report.to_dict(), ensure_ascii=False)
