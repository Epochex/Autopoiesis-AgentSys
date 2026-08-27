"""Business-facing verdicts over the real paired memory ablation.

The underlying executions come from ``memory_ablation.run_ablation``. Every arm
drives the same task orchestration service, context compiler, read-only probes,
verifier and memory store. This module adds a stable use-case contract and keeps
four conclusions separate: mechanism execution, measured business benefit,
harmful transfer, and action-safety coverage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.eval.memory_ablation import (
    AblationReport,
    CaseResult,
    hodges_lehmann_wilcoxon,
    mcnemar_mid_p,
    run_ablation,
)


_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_PATH = (
    _ROOT / "domains" / "network_rca" / "fixtures" / "memory_effectiveness_cases.json"
)
_REQUIRED_CASE_IDS = {
    "recurring_fault_value",
    "irrelevant_history_resistance",
    "fresh_healthy_override",
    "ownership_change_override",
    "static_runbook_comparison",
    "context_cost_accounting",
    "conflicting_root_supersession",
    "authorized_action_readback",
}


def load_effectiveness_catalog(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path is not None else _CATALOG_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("use_cases")
    if not isinstance(rows, list):
        raise ValueError("memory effectiveness catalog must contain a use_cases list")
    ids = [str(row.get("id", "")) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("memory effectiveness use-case ids must be unique")
    missing = sorted(_REQUIRED_CASE_IDS - set(ids))
    if missing:
        raise ValueError(f"memory effectiveness catalog is missing required cases: {missing}")
    return payload


def _by_pair(
    report: AblationReport,
    left: str,
    right: str,
    scenario_types: Iterable[str],
) -> list[tuple[CaseResult, CaseResult]]:
    allowed = set(scenario_types)
    right_rows = {
        row.pair_id: row
        for row in report.raw_results[right]
        if row.scenario_type in allowed
    }
    return [
        (row, right_rows[row.pair_id])
        for row in report.raw_results[left]
        if row.scenario_type in allowed and row.pair_id in right_rows
    ]


def _rate(rows: Sequence[CaseResult], field: str) -> float:
    if not rows:
        return 0.0
    return round(sum(bool(getattr(row, field)) for row in rows) / len(rows), 4)


def _paired_success(
    pairs: Sequence[tuple[CaseResult, CaseResult]], field: str
) -> dict[str, Any]:
    left = [row[0] for row in pairs]
    right = [row[1] for row in pairs]
    return {
        "paired_n": len(pairs),
        "left_rate": _rate(left, field),
        "right_rate": _rate(right, field),
        "left_only": sum(
            bool(getattr(l, field)) and not bool(getattr(r, field))
            for l, r in pairs
        ),
        "right_only": sum(not bool(getattr(l, field)) and bool(getattr(r, field)) for l, r in pairs),
    }


def _significant_success_advantage(comparison: dict[str, Any]) -> bool:
    left_only = int(comparison["left_only"])
    right_only = int(comparison["right_only"])
    return (
        left_only > right_only
        and mcnemar_mid_p(left_only, right_only) < 0.05
    )


def _tool_savings(
    pairs: Sequence[tuple[CaseResult, CaseResult]],
) -> dict[str, Any]:
    eligible = [
        (memory, baseline)
        for memory, baseline in pairs
        if memory.verified_repair and baseline.verified_repair
    ]
    differences = [baseline.tool_calls - memory.tool_calls for memory, baseline in eligible]
    summary = hodges_lehmann_wilcoxon([float(value) for value in differences])
    return {
        "direction": "baseline tool calls minus memory tool calls",
        "paired_n": len(eligible),
        "mean_saved": round(sum(differences) / len(differences), 4) if differences else None,
        "memory_wins": sum(value > 0 for value in differences),
        "baseline_wins": sum(value < 0 for value in differences),
        "ties": sum(value == 0 for value in differences),
        "paired_differences": differences,
        "hodges_lehmann_wilcoxon": summary,
    }


def _catalog_rows(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): dict(row) for row in catalog["use_cases"]}


def summarize_effectiveness(
    report: AblationReport,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = catalog or load_effectiveness_catalog()
    definitions = _catalog_rows(catalog)

    memory_vs_none = _by_pair(
        report, "M_memory", "A1_no_memory", ["ACT"]
    )
    sham_vs_none = _by_pair(
        report, "A2_sham_memory", "A1_no_memory", ["ACT"]
    )
    memory_vs_runbook = _by_pair(
        report, "M_memory", "A3_static_runbook", ["ACT"]
    )
    memory_rows = report.raw_results["M_memory"]
    healthy_rows = [row for row in memory_rows if row.scenario_type == "HOLD-healthy"]
    ownership_rows = [
        row for row in memory_rows if row.scenario_type == "HOLD-outofscope"
    ]

    recurrence_success = _paired_success(
        memory_vs_none, "verified_repair"
    )
    recurrence_tools = _tool_savings(memory_vs_none)
    tool_test = recurrence_tools["hodges_lehmann_wilcoxon"]
    recurrence_success["mcnemar_mid_p_two_sided"] = round(
        mcnemar_mid_p(
            recurrence_success["left_only"], recurrence_success["right_only"]
        ),
        6,
    )
    success_advantage = _significant_success_advantage(recurrence_success)
    tool_advantage = (
        (tool_test.get("hodges_lehmann") or 0.0) > 0
        and (tool_test.get("wilcoxon_p_two_sided") or 1.0) < 0.05
    )
    business_benefit = success_advantage or tool_advantage

    sham_correct = _paired_success(sham_vs_none, "root_cause_correct")
    sham_repair = _paired_success(sham_vs_none, "verified_repair")
    sham_harm = sum(
        max(0, sham.unnecessary_changes - baseline.unnecessary_changes)
        for sham, baseline in sham_vs_none
    )
    harmful_transfer_observed = (
        sham_correct["right_only"] > sham_correct["left_only"]
        or sham_repair["right_only"] > sham_repair["left_only"]
        or sham_harm > 0
    )

    context_metric = report.metrics["M2"]["comparisons"]["A1_no_memory"]["metrics"][
        "context_tokens"
    ]
    runbook_success = _paired_success(memory_vs_runbook, "verified_repair")
    runbook_success["mcnemar_mid_p_two_sided"] = round(
        mcnemar_mid_p(runbook_success["left_only"], runbook_success["right_only"]),
        6,
    )
    runbook_tools = _tool_savings(memory_vs_runbook)
    runbook_tool_test = runbook_tools["hodges_lehmann_wilcoxon"]
    runbook_advantage = _significant_success_advantage(runbook_success) or (
        (runbook_tool_test.get("hodges_lehmann") or 0.0) > 0
        and (runbook_tool_test.get("wilcoxon_p_two_sided") or 1.0) < 0.05
    )
    retrieved_count = sum(len(row.retrieved_memory_ids) for row in memory_rows)

    use_cases = [
        {
            **definitions["recurring_fault_value"],
            "status": "BENEFIT_DETECTED" if business_benefit else "BENEFIT_NOT_DEMONSTRATED",
            "observed": {
                "verified_repair": recurrence_success,
                "tool_calls": recurrence_tools,
            },
        },
        {
            **definitions["irrelevant_history_resistance"],
            "status": "HARM_OBSERVED" if harmful_transfer_observed else "NO_HARM_OBSERVED",
            "observed": {
                "root_cause_correct": sham_correct,
                "verified_repair": sham_repair,
                "extra_unnecessary_changes": sham_harm,
            },
        },
        {
            **definitions["fresh_healthy_override"],
            "status": "READ_ONLY_SIGNAL_ONLY",
            "observed": {
                "n": len(healthy_rows),
                "state_changes": sum(row.state_changes for row in healthy_rows),
                "unnecessary_changes": sum(row.unnecessary_changes for row in healthy_rows),
                "capability_eligible": sum(row.capability_eligible for row in healthy_rows),
            },
        },
        {
            **definitions["ownership_change_override"],
            "status": "READ_ONLY_SIGNAL_ONLY",
            "observed": {
                "n": len(ownership_rows),
                "state_changes": sum(row.state_changes for row in ownership_rows),
                "unnecessary_changes": sum(row.unnecessary_changes for row in ownership_rows),
                "capability_eligible": sum(row.capability_eligible for row in ownership_rows),
            },
        },
        {
            **definitions["static_runbook_comparison"],
            "status": (
                "ADVANTAGE_DETECTED"
                if runbook_advantage
                else "ADVANTAGE_NOT_DEMONSTRATED"
            ),
            "observed": {
                "verified_repair": runbook_success,
                "tool_calls": runbook_tools,
            },
        },
        {
            **definitions["context_cost_accounting"],
            "status": (
                "MEASURED_COST_INCREASE"
                if (context_metric.get("hodges_lehmann") or 0.0) > 0
                else "NO_MEASURED_COST_INCREASE"
            ),
            "observed": context_metric,
        },
        {
            **definitions["conflicting_root_supersession"],
            "status": "NOT_COVERED_END_TO_END",
            "observed": None,
        },
        {
            **definitions["authorized_action_readback"],
            "status": "NOT_COVERED_READ_ONLY_HARNESS",
            "observed": None,
        },
    ]

    return {
        "suite_id": catalog["suite_id"],
        "version": catalog["version"],
        "business_objective": catalog["business_objective"],
        "design": report.design,
        "verdicts": {
            "mechanism_execution": "EXERCISED" if retrieved_count > 0 else "NOT_EXERCISED",
            "business_effect": (
                "BENEFIT_DETECTED" if business_benefit else "BENEFIT_NOT_DEMONSTRATED"
            ),
            "harmful_transfer": (
                "OBSERVED" if harmful_transfer_observed else "NOT_OBSERVED_IN_THIS_SET"
            ),
            "action_safety": "NOT_COVERED_READ_ONLY_HARNESS",
        },
        "use_cases": use_cases,
        "external_anchors": catalog.get("external_anchors", []),
    }


def run_effectiveness_suite(
    *, repeats: int = 3, seed: int = 20_260_822
) -> dict[str, Any]:
    return summarize_effectiveness(
        run_ablation(repeats=repeats, seed=seed),
        load_effectiveness_catalog(),
    )


def render_summary(result: dict[str, Any]) -> str:
    verdicts = result["verdicts"]
    lines = [
        f"内部测试集：{result['suite_id']}",
        f"机制执行：{verdicts['mechanism_execution']}",
        f"业务收益：{verdicts['business_effect']}",
        f"负迁移：{verdicts['harmful_transfer']}",
        f"动作安全：{verdicts['action_safety']}",
        "",
        "Use cases:",
    ]
    for row in result["use_cases"]:
        lines.append(f"  {row['id']}: {row['status']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the network memory effectiveness suite"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20_260_822)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_effectiveness_suite(repeats=args.repeats, seed=args.seed)
    print(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.json
        else render_summary(result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "load_effectiveness_catalog",
    "render_summary",
    "run_effectiveness_suite",
    "summarize_effectiveness",
]
