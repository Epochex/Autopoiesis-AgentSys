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
    assert result["completed"] is False
    assert result["failed_step"] == "pricing_apply_policy"
    assert result["failure_phase"] == "post_execution_verification"
    assert result["manual_recovery_required"] is False
    assert verdict.checks["compensation"][0]["verified"] is True
    assert orchestrator._last_evidence == []


def test_illegal_status_transition_is_rejected_before_handler(tmp_path, monkeypatch):
    # Approving an unpriced draft skips the mandatory quote step: the precondition
    # must reject the illegal state transition before the write is trusted.
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "illegal_transition_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_bad_price")
    before = orchestrator.system_adapter.snapshot(case.id)
    calls = 0
    original = orchestrator.system_adapter.submit_approval

    def counted_submit(case_id):
        nonlocal calls
        calls += 1
        return original(case_id)

    monkeypatch.setattr(orchestrator.system_adapter, "submit_approval", counted_submit)

    result = execute_chain(["approval_submit", "status_check"], case, orchestrator)
    verdict = result["verdicts"][0]

    assert verdict.passed is False
    assert any("quote must be priced first" in violation for violation in verdict.violations)
    assert calls == 0
    assert result["results"] == []
    assert len(result["verdicts"]) == 1
    assert result["state"] == before
    assert result["failed_step"] == "approval_submit"
    assert result["failure_phase"] == "precondition"
    assert verdict.checks["handler"][0]["called"] is False


def test_failed_write_is_compensated_and_chain_does_not_continue(tmp_path, monkeypatch):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "stop_and_restore_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_bad_price")
    before = orchestrator.system_adapter.snapshot(case.id)
    approval_calls = 0
    original = orchestrator.system_adapter.submit_approval

    def counted_submit(case_id):
        nonlocal approval_calls
        approval_calls += 1
        return original(case_id)

    monkeypatch.setattr(orchestrator.system_adapter, "submit_approval", counted_submit)

    result = execute_chain(["pricing_apply_policy", "approval_submit"], case, orchestrator)

    assert result["completed"] is False
    assert result["failed_step"] == "pricing_apply_policy"
    assert approval_calls == 0
    assert len(result["results"]) == len(result["verdicts"]) == 1
    assert result["state"] == before
    recovery = result["verdicts"][0].checks["compensation"][0]
    assert recovery["attempted"] is True
    assert recovery["verified"] is True
    assert recovery["manual_recovery_required"] is False


def test_failed_write_without_restore_requires_manual_recovery(tmp_path):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "manual_recovery_trace.jsonl")
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_bad_price")
    backing_adapter = orchestrator.system_adapter

    class SnapshotOnlyAdapter:
        def snapshot(self, case_id):
            return backing_adapter.snapshot(case_id)

    # The registered handler still writes through the real backing adapter, but
    # the runtime has no compensation API and therefore must not claim rollback.
    orchestrator.system_adapter = SnapshotOnlyAdapter()

    result = execute_chain(["pricing_apply_policy"], case, orchestrator)

    assert result["completed"] is False
    assert result["manual_recovery_required"] is True
    assert result["state"]["pricing_status"] == "quoted"
    recovery = result["verdicts"][0].checks["compensation"][0]
    assert recovery["attempted"] is False
    assert recovery["verified"] is False
    assert recovery["manual_recovery_required"] is True
    assert "does not expose restore" in recovery["reason"]


def test_approval_required_needs_single_use_human_grant(tmp_path, monkeypatch):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "approval_gate.jsonl")
    case = load_enterprise_seed_cases()[0]
    orchestrator.system_adapter.apply_pricing(case.id)
    calls = 0
    original = orchestrator.system_adapter.submit_approval

    def counted(case_id):
        nonlocal calls
        calls += 1
        return original(case_id)

    monkeypatch.setattr(orchestrator.system_adapter, "submit_approval", counted)
    priced_state = orchestrator.system_adapter.snapshot(case.id)
    denied = execute_chain(
        ["approval_submit"], case.model_copy(update={"approval_grants": {}}), orchestrator
    )
    assert denied["completed"] is False
    assert denied["failure_phase"] == "approval"
    assert calls == 0

    allowed = execute_chain(["approval_submit"], case, orchestrator)
    assert allowed["completed"] is True
    assert calls == 1

    orchestrator.system_adapter.restore(case.id, priced_state)
    replayed = execute_chain(["approval_submit"], case, orchestrator)
    assert replayed["completed"] is False
    assert replayed["failure_phase"] == "approval"
    assert calls == 1


def test_step_judge_below_threshold_fails_the_step(tmp_path):
    # The step judge is an independent gate: even a contract-clean step fails when
    # the judge score falls below threshold.
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "judge_trace.jsonl")
    skill = orchestrator.skills.get("status_check")
    state = orchestrator.system_adapter.snapshot("ops_status_only")
    result = SkillResult(skill_name="status_check", readonly=True)
    verifier = ContractVerifier(step_judge=lambda step_context: 0.0)

    verdict = verifier.check_step(skill, state, {}, state, result)

    assert verdict.passed is False
    assert any("step judge score below threshold" in violation for violation in verdict.violations)
