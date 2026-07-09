from __future__ import annotations

from typing import Any

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec
from core.verifier.contracts import SkillContract, attach_contract


def register_enterprise_ops_skills(registry: SkillRegistry, adapter) -> None:
    specs = [
        SkillSpec(
            name="pricing_apply_policy",
            description="Apply enterprise pricing policy and write a quote",
            input_schema={"case_id": "str"},
            risk="write",
            cost=1.0,
            tags=["enterprise", "ops", "pricing", "price", "quote", "policy", "报价", "策略"],
        ),
        SkillSpec(
            name="approval_submit",
            description="Submit a quoted enterprise order for approval",
            input_schema={"case_id": "str"},
            risk="approval_required",
            cost=1.0,
            tags=["enterprise", "ops", "approval", "approve", "submit", "审批", "提交"],
        ),
        SkillSpec(
            name="status_check",
            description="Read enterprise order approval status",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=0.5,
            tags=["enterprise", "ops", "status", "state", "check", "状态", "查询"],
        ),
        SkillSpec(
            name="reminder_send",
            description="Send deterministic reminder for pending approval",
            input_schema={"case_id": "str"},
            risk="write",
            cost=0.75,
            tags=["enterprise", "ops", "reminder", "notify", "提醒"],
        ),
    ]
    for spec in specs:
        registry.register(spec, _handler(adapter, spec.name))
    _attach_contracts()


def _handler(adapter, skill_name: str):
    def run(case, state: dict[str, Any] | None = None) -> SkillResult:
        case_id = case.id
        if skill_name == "pricing_apply_policy":
            effect = adapter.apply_pricing(case_id)
            readonly = False
        elif skill_name == "approval_submit":
            effect = adapter.submit_approval(case_id)
            readonly = False
        elif skill_name == "status_check":
            effect = {"status": adapter.snapshot(case_id).get("status")}
            readonly = True
        elif skill_name == "reminder_send":
            effect = adapter.send_reminder(case_id)
            readonly = False
        else:
            raise ValueError(f"unknown enterprise skill: {skill_name}")
        return SkillResult(
            skill_name=skill_name,
            evidence=[
                {
                    "evidence_id": f"ev-{case_id}-{skill_name}",
                    "kind": "enterprise_ops_step",
                    "intended_effect": effect,
                }
            ],
            readonly=readonly,
            cost=0.5 if readonly else 1.0,
        )

    return run


def _attach_contracts() -> None:
    attach_contract(
        "pricing_apply_policy",
        SkillContract(
            preconditions=_pricing_preconditions,
            postconditions=_pricing_postconditions,
            invariants=_enterprise_invariants,
            write_like=True,
        ),
    )
    attach_contract(
        "approval_submit",
        SkillContract(
            preconditions=_approval_preconditions,
            postconditions=_approval_postconditions,
            invariants=_enterprise_invariants,
            write_like=True,
        ),
    )
    attach_contract(
        "status_check",
        SkillContract(
            preconditions=lambda state, args: [],
            postconditions=lambda before, after, result: [],
            invariants=_enterprise_invariants,
            write_like=False,
        ),
    )
    attach_contract(
        "reminder_send",
        SkillContract(
            preconditions=_reminder_preconditions,
            postconditions=_reminder_postconditions,
            invariants=_enterprise_invariants,
            write_like=True,
        ),
    )


def _pricing_preconditions(state: dict[str, Any], args: dict[str, Any]) -> list[str]:
    violations = []
    if float(state.get("base_price", 0)) <= 0:
        violations.append("pricing precondition failed: base_price must be positive")
    if "policy" not in state:
        violations.append("pricing precondition failed: policy missing")
    return violations


def _pricing_postconditions(before: dict[str, Any], after: dict[str, Any], result) -> list[str]:
    violations = []
    quote = after.get("quote_price")
    policy = before.get("policy", {})
    if after.get("pricing_status") != "quoted":
        violations.append("pricing postcondition failed: pricing_status must be quoted")
    if quote is None:
        violations.append("pricing postcondition failed: quote_price missing")
    else:
        min_price = float(policy.get("min_price", 0))
        max_price = float(policy.get("max_price", float("inf")))
        if not (min_price <= float(quote) <= max_price):
            violations.append("pricing postcondition failed: price out of range")
    return violations


def _approval_preconditions(state: dict[str, Any], args: dict[str, Any]) -> list[str]:
    violations = []
    if state.get("pricing_status") != "quoted":
        violations.append("approval precondition failed: quote must be priced first")
    if state.get("quote_price") is None:
        violations.append("approval precondition failed: quote_price missing")
    return violations


def _approval_postconditions(before: dict[str, Any], after: dict[str, Any], result) -> list[str]:
    violations = []
    if after.get("approval_submitted") is not True:
        violations.append("approval postcondition failed: approval_submitted must be true")
    if after.get("status") != "pending_approval":
        violations.append("approval postcondition failed: status must be pending_approval")
    return violations


def _reminder_preconditions(state: dict[str, Any], args: dict[str, Any]) -> list[str]:
    if state.get("status") != "pending_approval":
        return ["reminder precondition failed: status must be pending_approval"]
    return []


def _reminder_postconditions(before: dict[str, Any], after: dict[str, Any], result) -> list[str]:
    if after.get("reminder_sent") is not True:
        return ["reminder postcondition failed: reminder_sent must be true"]
    return []


def _enterprise_invariants(state: dict[str, Any]) -> list[str]:
    violations = []
    status = state.get("status")
    if status == "approved" and state.get("approval_submitted") is not True:
        violations.append("invariant failed: approved status requires submitted approval")
    quote = state.get("quote_price")
    if quote is not None and float(quote) < 0:
        violations.append("invariant failed: quote_price cannot be negative")
    return violations
