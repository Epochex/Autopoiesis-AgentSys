from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from domains.active_recon.factory import (
    build_active_recon_orchestrator,
    load_recon_ground_truth,
    load_recon_seed_cases,
)
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases


def run_cross_domain_eval() -> dict[str, Any]:
    """Run the unchanged RCA orchestrator kernel on both mock domain seed sets."""
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        network_orch = build_network_rca_orchestrator(root / "network_rca_trace.jsonl", data_source="mock")
        recon_orch = build_active_recon_orchestrator(root / "active_recon_trace.jsonl")

        orchestrator_class = type(network_orch).__name__
        assert orchestrator_class == type(recon_orch).__name__

        domains = {
            "network_rca": _eval_network_rca(network_orch),
            "active_recon": _eval_active_recon(recon_orch),
        }
        for row in domains.values():
            row["orchestrator_class"] = orchestrator_class

        return {
            "domains": domains,
            "kernel_reuse": {
                "orchestrator_class": orchestrator_class,
                "domains": len(domains),
                "kernel_changes": 0,
                "dataset_kind": "mock",
                "summary": f"same kernel class drove {len(domains)} domains (mock sets)",
            },
        }


def _eval_network_rca(orchestrator) -> dict[str, Any]:
    cases = load_seed_cases()
    ground_truth = load_ground_truth()
    correct = 0
    passed = 0

    for case in cases:
        diagnosis, report = orchestrator.diagnose(case)
        truth = ground_truth[case.id]
        correct += int(diagnosis.root_cause_key == truth.expected_root_cause_key)
        passed += int(report.passed)

    total = len(cases)
    root_cause_accuracy = round(correct / total, 4) if total else 0.0
    return {
        "dataset_kind": "mock",
        "cases": total,
        "primary_metric": "root_cause_accuracy",
        "primary_acc": root_cause_accuracy,
        "root_cause_accuracy": root_cause_accuracy,
        "verifier_pass_rate": round(passed / total, 4) if total else 0.0,
    }


def _eval_active_recon(orchestrator) -> dict[str, Any]:
    cases = load_recon_seed_cases()
    ground_truth = load_recon_ground_truth()
    correct = 0
    passed = 0

    for case in cases:
        diagnosis, report = orchestrator.diagnose(case)
        truth = ground_truth[case.id]
        correct += int(diagnosis.root_cause_key == truth.top_risk)
        passed += int(report.passed)

    total = len(cases)
    top_risk_accuracy = round(correct / total, 4) if total else 0.0
    return {
        "dataset_kind": "mock",
        "cases": total,
        "primary_metric": "top_risk_accuracy",
        "primary_acc": top_risk_accuracy,
        "top_risk_accuracy": top_risk_accuracy,
        "verifier_pass_rate": round(passed / total, 4) if total else 0.0,
    }


def _print_table(result: dict[str, Any]) -> None:
    headers = ["domain", "cases", "primary_acc", "verifier_pass"]
    print(" | ".join(headers))
    print(" | ".join("-" * len(header) for header in headers))
    for domain, row in result["domains"].items():
        label = f"{domain}_{row['dataset_kind']}"
        print(
            " | ".join(
                [
                    label,
                    str(row["cases"]),
                    f"{row['primary_acc']:.4f}",
                    f"{row['verifier_pass_rate']:.4f}",
                ]
            )
        )
    print(result["kernel_reuse"]["summary"])


if __name__ == "__main__":
    _print_table(run_cross_domain_eval())
