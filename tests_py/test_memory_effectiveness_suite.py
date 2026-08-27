from __future__ import annotations

from core.eval.memory_effectiveness_suite import (
    load_effectiveness_catalog,
    run_effectiveness_suite,
)


def test_effectiveness_catalog_has_stable_use_case_ids() -> None:
    catalog = load_effectiveness_catalog()
    ids = {row["id"] for row in catalog["use_cases"]}
    assert ids == {
        "recurring_fault_value",
        "irrelevant_history_resistance",
        "fresh_healthy_override",
        "ownership_change_override",
        "static_runbook_comparison",
        "context_cost_accounting",
        "conflicting_root_supersession",
        "authorized_action_readback",
    }


def test_current_suite_separates_execution_value_harm_and_coverage() -> None:
    result = run_effectiveness_suite(repeats=3, seed=20_260_822)

    assert result["verdicts"] == {
        "mechanism_execution": "EXERCISED",
        "business_effect": "BENEFIT_NOT_DEMONSTRATED",
        "harmful_transfer": "NOT_OBSERVED_IN_THIS_SET",
        "action_safety": "NOT_COVERED_READ_ONLY_HARNESS",
    }
    status = {row["id"]: row["status"] for row in result["use_cases"]}
    assert status["irrelevant_history_resistance"] == "NO_HARM_OBSERVED"
    assert status["context_cost_accounting"] == "MEASURED_COST_INCREASE"
    assert status["conflicting_root_supersession"] == "NOT_COVERED_END_TO_END"
    assert status["authorized_action_readback"] == "NOT_COVERED_READ_ONLY_HARNESS"
