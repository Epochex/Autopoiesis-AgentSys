from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


ViolationFn = Callable[[dict[str, Any], dict[str, Any]], list[str]]
PostconditionFn = Callable[[dict[str, Any], dict[str, Any], Any], list[str]]
InvariantFn = Callable[[dict[str, Any]], list[str]]
StepJudgeFn = Callable[[dict[str, Any]], float]


@dataclass
class SkillContract:
    preconditions: ViolationFn | None = None
    postconditions: PostconditionFn | None = None
    invariants: InvariantFn | None = None
    intended_effect: Callable[[Any], dict[str, Any]] | None = None
    write_like: bool = False


class StepVerdict(BaseModel):
    passed: bool
    violations: list[str] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)


_CONTRACTS: dict[str, SkillContract] = {}


def attach_contract(skill_name: str, contract: SkillContract) -> None:
    _CONTRACTS[skill_name] = contract


def get_contract(skill_or_name: Any) -> SkillContract | None:
    if isinstance(skill_or_name, str):
        return _CONTRACTS.get(skill_or_name)
    spec = getattr(skill_or_name, "spec", skill_or_name)
    name = getattr(spec, "name", None)
    return _CONTRACTS.get(str(name)) if name else None


def grounded_readback(intended_effect: dict[str, Any], observed_state: dict[str, Any]) -> bool:
    """Confirm an intended write is visible in the observed mock-system state."""
    if not intended_effect:
        return False
    for key, expected in intended_effect.items():
        observed = observed_state.get(key)
        if isinstance(expected, dict):
            if not isinstance(observed, dict) or not grounded_readback(expected, observed):
                return False
        elif observed != expected:
            return False
    return True


def default_step_judge(step_context: dict[str, Any]) -> float:
    """Deterministic hook standing in for a future LLM judge."""
    skill = step_context.get("skill")
    result = step_context.get("result")
    violations = step_context.get("violations") or []
    if violations:
        return 0.0
    expected_name = getattr(getattr(skill, "spec", skill), "name", "")
    result_name = getattr(result, "skill_name", expected_name)
    return 1.0 if not expected_name or result_name == expected_name else 0.25


@dataclass
class ContractVerifier:
    step_judge: StepJudgeFn = default_step_judge
    judge_threshold: float = 0.5
    contracts: dict[str, SkillContract] = field(default_factory=lambda: _CONTRACTS)

    def check_step(
        self,
        skill: Any,
        state_before: dict[str, Any],
        args: dict[str, Any],
        state_after: dict[str, Any],
        result: Any,
    ) -> StepVerdict:
        contract = self._contract_for(skill)
        checks: dict[str, Any] = {
            "preconditions": [],
            "postconditions": [],
            "invariants": [],
            "grounded_readback": [],
            "judge": [],
        }
        violations: list[str] = []

        if contract is not None:
            if contract.preconditions is not None:
                checks["preconditions"] = list(contract.preconditions(state_before, args))
                violations.extend(checks["preconditions"])
            if contract.postconditions is not None:
                checks["postconditions"] = list(contract.postconditions(state_before, state_after, result))
                violations.extend(checks["postconditions"])
            if contract.invariants is not None:
                before = [f"before: {item}" for item in contract.invariants(state_before)]
                after = [f"after: {item}" for item in contract.invariants(state_after)]
                checks["invariants"] = before + after
                violations.extend(checks["invariants"])

            intended = self._intended_effect(contract, result)
            if contract.write_like or intended:
                if not grounded_readback(intended, state_after):
                    message = "claimed but not landed: intended effect absent from observed state"
                    checks["grounded_readback"] = [message]
                    violations.append(message)

        step_context = {
            "skill": skill,
            "state_before": state_before,
            "args": args,
            "state_after": state_after,
            "result": result,
            "violations": violations,
        }
        score = float(self.step_judge(step_context))
        checks["judge"] = [{"score": round(score, 4), "threshold": self.judge_threshold}]
        if score < self.judge_threshold:
            violations.append(f"step judge score below threshold: {score:.4f}")

        return StepVerdict(passed=not violations, violations=violations, checks=checks)

    def _contract_for(self, skill: Any) -> SkillContract | None:
        spec = getattr(skill, "spec", skill)
        name = getattr(spec, "name", None)
        return self.contracts.get(str(name)) if name else None

    @staticmethod
    def _intended_effect(contract: SkillContract, result: Any) -> dict[str, Any]:
        if contract.intended_effect is not None:
            return dict(contract.intended_effect(result))
        for item in getattr(result, "evidence", []) or []:
            intended = item.get("intended_effect")
            if isinstance(intended, dict):
                return dict(intended)
        return {}
