from __future__ import annotations

from core.orchestrator.planner import execute_chain, plan_skill_chain
from domains.enterprise_ops.eval import run_eval
from domains.enterprise_ops.factory import build_enterprise_ops_orchestrator, load_enterprise_seed_cases


def test_enterprise_ops_domain_registers_reusable_capabilities(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "enterprise_trace.jsonl")

    names = {skill.spec.name for skill in orchestrator.skills.all()}

    assert {"pricing_apply_policy", "approval_submit", "status_check", "reminder_send"}.issubset(names)


def test_enterprise_ops_chain_executes_with_process_verdicts(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "enterprise_chain_trace.jsonl")
    case = load_enterprise_seed_cases()[0]

    result = execute_chain(plan_skill_chain(case.query, orchestrator.skills), case, orchestrator)

    assert result["chain"] == ["pricing_apply_policy", "approval_submit"]
    assert [verdict.passed for verdict in result["verdicts"]] == [True, True]
    assert result["state"]["approval_submitted"] is True


def test_enterprise_ops_eval_print_data_has_chains_and_violations():
    rows = run_eval()

    assert rows
    assert all(row["chain"] for row in rows)
    assert any("price out of range" in " ".join(row["violations"]) for row in rows)
