from __future__ import annotations

from core.orchestrator.planner import execute_chain
from core.skills.spec import SkillResult
from core.verifier.contracts import ContractVerifier, grounded_readback
from domains.enterprise_ops.factory import build_enterprise_ops_orchestrator, load_enterprise_seed_cases


def test_postcondition_violation_is_caught_at_offending_step(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "bad_price_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_bad_price")

    result = execute_chain(["pricing_apply_policy"], case, orchestrator)
    verdict = result["verdicts"][0]

    assert verdict.passed is False
    assert any("price out of range" in violation for violation in verdict.violations)


def test_invariant_violation_is_caught_at_step_verifier(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "invariant_trace.jsonl")
    case_id = "ops_illegal_approved"
    before = orchestrator.system_adapter.snapshot(case_id)
    orchestrator.system_adapter.mark_approved(case_id)
    after = orchestrator.system_adapter.snapshot(case_id)
    skill = orchestrator.skills.get("status_check")
    result = SkillResult(skill_name="status_check", readonly=True)

    verdict = ContractVerifier().check_step(skill, before, {}, after, result)

    assert verdict.passed is False
    assert any("approved status requires submitted approval" in violation for violation in verdict.violations)


def test_claimed_but_not_landed_write_is_caught_by_grounded_readback(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "readback_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_quote_then_approval")
    orchestrator.system_adapter.drop_next_write = True

    result = execute_chain(["pricing_apply_policy"], case, orchestrator)
    verdict = result["verdicts"][0]

    assert grounded_readback({"quote_price": 104.0}, result["state"]) is False
    assert verdict.passed is False
    assert any("claimed but not landed" in violation for violation in verdict.violations)
