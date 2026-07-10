from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from core.orchestrator.planner import execute_chain, plan_skill_chain
from domains.enterprise_ops.factory import (
    build_enterprise_intent_router,
    build_enterprise_ops_orchestrator,
    load_enterprise_seed_cases,
)
from domains.enterprise_ops.schema import EnterpriseOpsCase


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


def run_routed_eval(induction_store: str | Path | None = None) -> list[dict]:
    """Route every case through the CascadingIntentRouter, then execute resolved chains.

    The seed workflows resolve at the rule fast path; `unmatched_capability_case`
    matches nothing and exercises the miss -> capture -> induce -> promote ->
    re-route tier live. The cascade per case is replayable from the trace ledger.
    """
    rows = []
    with TemporaryDirectory() as tmp_dir:
        orchestrator = build_enterprise_ops_orchestrator(Path(tmp_dir) / "routed_trace.jsonl")
        router = build_enterprise_intent_router(
            orchestrator,
            induction_store=Path(induction_store) if induction_store is not None else Path(tmp_dir) / "induction_captures.jsonl",
        )
        for case in [*load_enterprise_seed_cases(), unmatched_capability_case()]:
            outcome = router.route(case)
            row = {
                "case_id": case.id,
                "tier": outcome.tier,
                "resolved": outcome.resolved,
                "induced": outcome.induced,
                "chain": list(outcome.chain),
                "executed": False,
                "violations": [],
            }
            if outcome.resolved and outcome.chain and orchestrator.system_adapter.has_case(case.id):
                result = execute_chain(outcome.chain, case, orchestrator)
                row["executed"] = True
                row["verdicts"] = [verdict.model_dump() for verdict in result["verdicts"]]
                row["violations"] = [violation for verdict in result["verdicts"] for violation in verdict.violations]
            rows.append(row)
    return rows


def unmatched_capability_case() -> EnterpriseOpsCase:
    """A request no current skill covers — drives the miss->induction tier."""
    return EnterpriseOpsCase(
        id="ops_unmatched_inventory",
        query="inventory restock plan for overseas warehouse",
        query_terms=["inventory", "restock", "warehouse"],
        assets=["warehouse-88"],
        relevant_skills=[],
    )


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


def _print_routed_rows(rows: list[dict]) -> None:
    print("cascading intent routing:")
    for row in rows:
        chain = " -> ".join(row["chain"]) if row["chain"] else "-"
        print(
            f"case={row['case_id']} tier={row['tier']} resolved={row['resolved']} "
            f"induced={row['induced']} chain=[{chain}] executed={row['executed']}"
        )
        if row["violations"]:
            print(f"  caught violations: {row['violations']}")
    print()


if __name__ == "__main__":
    _print_rows(run_eval())
    _print_routed_rows(run_routed_eval())
