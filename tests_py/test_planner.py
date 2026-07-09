from __future__ import annotations

from core.orchestrator.planner import execute_chain, plan_skill_chain
from core.trace.ledger import JSONLTraceLedger
from domains.enterprise_ops.factory import build_enterprise_ops_orchestrator, load_enterprise_seed_cases


def test_two_domain_request_plans_ordered_chain_and_executes(tmp_path):
    ledger_path = tmp_path / "planner_trace.jsonl"
    orchestrator = build_enterprise_ops_orchestrator(ledger_path)
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_quote_then_approval")

    chain = plan_skill_chain(case.query, orchestrator.skills)
    result = execute_chain(chain, case, orchestrator)

    assert chain[:2] == ["pricing_apply_policy", "approval_submit"]
    assert len(chain) >= 2
    assert all(verdict.passed for verdict in result["verdicts"])
    assert result["state"]["status"] == "pending_approval"
    events = JSONLTraceLedger(ledger_path).replay()
    assert [event.kind for event in events].count("step_verified") == len(chain)


def test_single_domain_request_yields_one_skill_chain(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "single_domain_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_status_only")

    chain = plan_skill_chain(case.query, orchestrator.skills)

    assert chain == ["status_check"]
