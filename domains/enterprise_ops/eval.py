from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from core.orchestrator.planner import execute_chain, plan_skill_chain
from domains.enterprise_ops.factory import build_enterprise_ops_orchestrator, load_enterprise_seed_cases


def run_eval() -> list[dict]:
    rows = []
    with TemporaryDirectory() as tmp_dir:
        orchestrator = build_enterprise_ops_orchestrator(Path(tmp_dir) / "enterprise_ops_trace.jsonl")
        for case in load_enterprise_seed_cases():
            chain = plan_skill_chain(case.query, orchestrator.skills)
            result = execute_chain(chain, case, orchestrator)
            rows.append(
                {
                    "case_id": case.id,
                    "chain": chain,
                    "verdicts": [verdict.model_dump() for verdict in result["verdicts"]],
                    "violations": [violation for verdict in result["verdicts"] for violation in verdict.violations],
                }
            )
    return rows


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(f"case={row['case_id']}")
        print(f"chain executed: {' -> '.join(row['chain'])}")
        for index, verdict in enumerate(row["verdicts"], start=1):
            status = "passed" if verdict["passed"] else "failed"
            print(f"step {index}: {status} violations={verdict['violations']}")
        if row["violations"]:
            print(f"caught violations: {row['violations']}")
        print()


if __name__ == "__main__":
    _print_rows(run_eval())
