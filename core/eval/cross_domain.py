"""Cross-domain kernel-reuse eval: the identical orchestrator class must drive
both mock seed domains with zero kernel changes. LLM-free and deterministic."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence

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
        if orchestrator_class != type(recon_orch).__name__:
            raise RuntimeError(
                "kernel reuse violated: domains built different orchestrator classes "
                f"({orchestrator_class} vs {type(recon_orch).__name__})"
            )

        domains = {
            "network_rca": _eval_domain(
                network_orch,
                load_seed_cases(),
                load_ground_truth(),
                metric="root_cause_accuracy",
                expected_key=lambda truth: truth.expected_root_cause_key,
            ),
            "active_recon": _eval_domain(
                recon_orch,
                load_recon_seed_cases(),
                load_recon_ground_truth(),
                metric="top_risk_accuracy",
                expected_key=lambda truth: truth.top_risk,
            ),
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


def _eval_domain(
    orchestrator,
    cases: Sequence[Any],
    ground_truth: Mapping[str, Any],
    *,
    metric: str,
    expected_key: Callable[[Any], str],
) -> dict[str, Any]:
    """Accuracy of ``diagnosis.root_cause_key`` against the domain's expected key,
    plus verifier pass rate. ``metric`` names the domain's primary-accuracy column."""
    correct = 0
    passed = 0
    for case in cases:
        diagnosis, report = orchestrator.diagnose(case)
        truth = ground_truth[case.id]
        correct += int(diagnosis.root_cause_key == expected_key(truth))
        passed += int(report.passed)

    total = len(cases)
    accuracy = round(correct / total, 4) if total else 0.0
    return {
        "dataset_kind": "mock",
        "cases": total,
        "primary_metric": metric,
        "primary_acc": accuracy,
        metric: accuracy,
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
