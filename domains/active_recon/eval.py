from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel

from domains.active_recon.factory import (
    build_active_recon_orchestrator,
    load_recon_ground_truth,
    load_recon_seed_cases,
)
from domains.active_recon.schema import ReconCase, ReconGroundTruth
from domains.active_recon.situational import build_situational_picture


class ReconEvalRow(BaseModel):
    name: str
    cases: int
    top_risk_accuracy: float
    exposure_recall: float
    verifier_pass_rate: float


def run_eval(
    cases: list[ReconCase] | None = None,
    ground_truth: dict[str, ReconGroundTruth] | None = None,
) -> list[ReconEvalRow]:
    cases = cases or load_recon_seed_cases()
    ground_truth = ground_truth or load_recon_ground_truth()
    with TemporaryDirectory() as tmp_dir:
        orchestrator = build_active_recon_orchestrator(Path(tmp_dir) / "active_recon_trace.jsonl")
        correct = 0
        recall_total = 0.0
        passed = 0
        for case in cases:
            diagnosis, report = orchestrator.diagnose(case)
            truth = ground_truth[case.id]
            picture = build_situational_picture(orchestrator._last_evidence)
            observed = {exposure["service"] for exposure in picture["exposures"]}
            expected = set(truth.exposed_services)
            correct += int(diagnosis.root_cause_key == truth.top_risk)
            recall_total += len(expected.intersection(observed)) / len(expected) if expected else 1.0
            passed += int(report.passed)
        total = len(cases)
        return [
            ReconEvalRow(
                name="active_recon_mock",
                cases=total,
                top_risk_accuracy=round(correct / total, 4) if total else 0.0,
                exposure_recall=round(recall_total / total, 4) if total else 0.0,
                verifier_pass_rate=round(passed / total, 4) if total else 0.0,
            )
        ]


def _print_table(rows: list[ReconEvalRow]) -> None:
    headers = ["name", "cases", "top_risk_accuracy", "exposure_recall", "verifier_pass_rate"]
    print(" | ".join(headers))
    print(" | ".join("-" * len(header) for header in headers))
    for row in rows:
        print(
            " | ".join(
                [
                    row.name,
                    str(row.cases),
                    f"{row.top_risk_accuracy:.4f}",
                    f"{row.exposure_recall:.4f}",
                    f"{row.verifier_pass_rate:.4f}",
                ]
            )
        )


if __name__ == "__main__":
    _print_table(run_eval())
