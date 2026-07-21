from __future__ import annotations

import inspect
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from core.verifier.contracts import ContractVerifier, StepVerdict, get_contract


# rule fast path: connectors that split a composite request into ordered subgoals
_CONNECTORS = ["通过就", "然后", "再", "并且", "，", ",", ";", " and ", " then "]
# bilingual lexical bridge so Chinese requests recall English-tagged skills
_SYNONYMS = {
    "报价": {"pricing", "price", "quote", "policy"},
    "价格": {"pricing", "price", "quote"},
    "新策略": {"policy", "pricing"},
    "审批": {"approval", "approve", "submit"},
    "提交": {"submit", "approval"},
    "通过": {"approve", "approval"},
    "状态": {"status", "state"},
    "查询": {"check", "status"},
    "提醒": {"reminder", "notify"},
}


class ChainPlan(BaseModel):
    """Rule-fast-path decomposition report: the tier-1 routing evidence.

    `coverage` (matched subgoals / all subgoals) and `match_hits` (term-overlap
    strength per selected skill) are the router's confidence signals; a
    whole-request fallback match never counts as subgoal coverage, and a
    weakest-link 1-token match is distinguishable from a genuine one.
    """

    chain: list[str] = Field(default_factory=list)
    subgoals: list[str] = Field(default_factory=list)
    matched_subgoals: int = 0
    unmatched_subgoals: list[str] = Field(default_factory=list)
    match_hits: list[int] = Field(default_factory=list)
    used_fallback: bool = False

    @property
    def coverage(self) -> float:
        return self.matched_subgoals / len(self.subgoals) if self.subgoals else 0.0

    @property
    def full_coverage(self) -> bool:
        return bool(self.subgoals) and self.matched_subgoals == len(self.subgoals)

    @property
    def min_match_hits(self) -> int:
        return min(self.match_hits) if self.match_hits else 0

    @property
    def max_match_hits(self) -> int:
        return max(self.match_hits) if self.match_hits else 0


def plan_skill_chain(request: str, registry: SkillRegistry) -> list[str]:
    """Rule fast path: decompose `request` into subgoals and pick one skill per subgoal.

    Returns an ordered, duplicate-free chain; an empty list is the routing-miss
    signal (nothing in the library matched — the caller may escalate or capture
    the request for skill induction).
    """
    return plan_skill_chain_detailed(request, registry).chain


def plan_skill_chain_detailed(request: str, registry: SkillRegistry) -> ChainPlan:
    """Same planning pass as `plan_skill_chain`, plus per-subgoal coverage evidence."""
    subgoals = _decompose(request)
    chain: list[str] = []
    hits: list[int] = []
    unmatched: list[str] = []
    for subgoal in subgoals:
        terms = _expand_terms(subgoal)
        selected = _best_skill_for_terms(terms, registry.all(), exclude=set(chain))
        if selected is not None:
            chain.append(selected[0])
            hits.append(selected[1])
        else:
            unmatched.append(subgoal)
    matched = len(subgoals) - len(unmatched)
    used_fallback = False
    if not chain:
        selected = _best_skill_for_terms(_expand_terms(request), registry.all(), exclude=set())
        if selected is not None:
            chain.append(selected[0])
            hits.append(selected[1])
            used_fallback = True
    return ChainPlan(
        chain=chain,
        subgoals=subgoals,
        matched_subgoals=matched,
        unmatched_subgoals=unmatched,
        match_hits=hits,
        used_fallback=used_fallback,
    )


def execute_chain(chain: list[str], case: Any, orchestrator: Any) -> dict[str, Any]:
    """Execute a planned chain step by step under contract verification.

    `orchestrator` is duck-typed: needs `skills` and `ledger`, plus optional
    `contract_verifier` and `system_adapter`/`adapter` (for state snapshots).
    Preconditions are checked before a handler can create side effects.  A
    handler result is trusted only after postconditions, invariants, and
    grounded readback pass against a fresh system snapshot.  The chain stops at
    the first failure.  Failed writes are restored through the adapter's real
    ``restore(case_id, state)`` capability when available, and that restoration
    is itself read back before it is reported as successful.

    Returns the requested ``chain``, executed ``results``, ``verdicts``, final
    ``state``, and explicit completion/recovery fields. Raises KeyError for an
    unknown skill in `chain`.
    """
    run_id = str(uuid4())
    case_id = str(getattr(case, "id", "chain_case"))
    orchestrator.last_run_id = run_id
    orchestrator._run_events = []
    _record(orchestrator, run_id, case_id, "skill_chain_planned", {"chain": list(chain), "query": getattr(case, "query", "")})

    verifier = getattr(orchestrator, "contract_verifier", None) or ContractVerifier()
    state = _snapshot(orchestrator, case)
    results = []
    trusted_results = []
    verdicts: list[StepVerdict] = []
    failed_step: str | None = None
    failure_phase: str | None = None
    manual_recovery_required = False

    for index, name in enumerate(chain, start=1):
        skill = orchestrator.skills.get(name)
        state_before = deepcopy(state)
        args = {"case": case, "state": deepcopy(state_before)}

        precondition_verdict = _check_preconditions(verifier, skill, state_before, args)
        if precondition_verdict is not None:
            verdicts.append(precondition_verdict)
            failed_step = name
            failure_phase = "precondition"
            _record_step_verdict(
                orchestrator,
                run_id,
                case_id,
                name,
                index,
                precondition_verdict,
                handler_called=False,
            )
            break

        risk_verdict = _check_risk_gate(orchestrator, skill, case)
        if risk_verdict is not None:
            verdicts.append(risk_verdict)
            failed_step = name
            failure_phase = "approval"
            _record(
                orchestrator,
                run_id,
                case_id,
                "human_approval_rejected",
                {"skill": name, "chain_step": index, "violations": list(risk_verdict.violations)},
            )
            _record_step_verdict(
                orchestrator, run_id, case_id, name, index, risk_verdict, handler_called=False
            )
            break
        if getattr(skill.spec, "risk", "read_only") == "approval_required":
            _record(
                orchestrator,
                run_id,
                case_id,
                "human_approval_granted",
                {"skill": name, "chain_step": index, "single_use": True},
            )

        try:
            result = _execute_skill(orchestrator.skills, name, args)
        except Exception as exc:
            # A handler can fail after a remote write.  Observe the system before
            # deciding whether compensation is necessary; never infer rollback
            # from the exception alone.
            state_after_error = _snapshot(orchestrator, case)
            verdict = StepVerdict(
                passed=False,
                violations=[f"handler execution failed: {type(exc).__name__}: {exc}"],
                checks={"handler": [{"called": True, "raised": type(exc).__name__}]},
            )
            contract = _contract_for(verifier, skill)
            if contract is not None and contract.write_like and state_after_error != state_before:
                recovery, observed_state = _recover_write(orchestrator, case, state_before)
                verdict.checks["compensation"] = [recovery]
                manual_recovery_required = not recovery["verified"]
                state = observed_state
            else:
                verdict.checks["compensation"] = [{"attempted": False, "verified": False, "reason": "no observed write"}]
                state = state_after_error
            verdicts.append(verdict)
            failed_step = name
            failure_phase = "handler"
            _record(
                orchestrator,
                run_id,
                case_id,
                "tool_called",
                {
                    "skill": name,
                    "chain_step": index,
                    "failed": True,
                    "error_type": type(exc).__name__,
                },
            )
            _record_step_verdict(orchestrator, run_id, case_id, name, index, verdict, handler_called=True)
            break

        state_after = _snapshot(orchestrator, case)
        verdict = verifier.check_step(skill, state_before, args, state_after, result)
        results.append(result)

        _record(
            orchestrator,
            run_id,
            case_id,
            "tool_called",
            {
                "skill": name,
                "readonly": bool(result.readonly),
                "evidence_ids": [item.get("evidence_id") for item in result.evidence],
                "cost": result.cost,
                "chain_step": index,
            },
        )
        if verdict.passed:
            state = state_after
            trusted_results.append(result)
        else:
            failed_step = name
            failure_phase = "post_execution_verification"
            contract = _contract_for(verifier, skill)
            write_like = bool(contract is not None and contract.write_like) or not bool(result.readonly)
            if write_like:
                recovery, observed_state = _recover_write(orchestrator, case, state_before)
                verdict.checks["compensation"] = [recovery]
                manual_recovery_required = not recovery["verified"]
                state = observed_state
            else:
                verdict.checks["compensation"] = [{"attempted": False, "verified": False, "reason": "read-only step"}]
                state = state_after

        verdicts.append(verdict)
        _record_step_verdict(orchestrator, run_id, case_id, name, index, verdict, handler_called=True)
        if not verdict.passed:
            break

    # Evidence from a rejected or compensated write must not be available to a
    # downstream reasoner as though that write had committed.
    orchestrator._last_evidence = [item for step_result in trusted_results for item in step_result.evidence]
    return {
        "chain": list(chain),
        "results": results,
        "verdicts": verdicts,
        "state": state,
        "completed": failed_step is None,
        "failed_step": failed_step,
        "failure_phase": failure_phase,
        "manual_recovery_required": manual_recovery_required,
    }


def _check_preconditions(
    verifier: ContractVerifier,
    skill: Any,
    state_before: dict[str, Any],
    args: dict[str, Any],
) -> StepVerdict | None:
    """Return a failing verdict before execution, or ``None`` when safe to call."""
    contract = _contract_for(verifier, skill)
    if contract is None or contract.preconditions is None:
        return None
    violations = list(contract.preconditions(state_before, args))
    if not violations:
        return None
    return StepVerdict(
        passed=False,
        violations=violations,
        checks={
            "preconditions": violations,
            "handler": [{"called": False, "reason": "precondition failed"}],
            "postconditions": [],
            "invariants": [],
            "grounded_readback": [],
            "judge": [],
            "compensation": [{"attempted": False, "verified": False, "reason": "handler not called"}],
        },
    )


def _check_risk_gate(orchestrator: Any, skill: Any, case: Any) -> StepVerdict | None:
    """Consume a case-bound, single-use grant for approval-required skills."""
    spec = getattr(skill, "spec", skill)
    if getattr(spec, "risk", "read_only") != "approval_required":
        return None
    skill_name = str(getattr(spec, "name", ""))
    grants = getattr(case, "approval_grants", {}) or {}
    grant = str(grants.get(skill_name, "")).strip()
    consumed = getattr(orchestrator, "_consumed_approval_grants", None)
    if consumed is None:
        consumed = set()
        orchestrator._consumed_approval_grants = consumed
    violation: str | None = None
    if not grant:
        violation = f"human approval required for {skill_name}"
    elif grant in consumed:
        violation = f"human approval grant already consumed for {skill_name}"
    if violation is not None:
        return StepVerdict(
            passed=False,
            violations=[violation],
            checks={
                "approval": [{"granted": False, "single_use": True}],
                "handler": [{"called": False, "reason": "approval gate failed"}],
                "compensation": [{"attempted": False, "verified": False, "reason": "handler not called"}],
            },
        )
    consumed.add(grant)
    return None


def _contract_for(verifier: ContractVerifier, skill: Any):
    """Resolve the same contract mapping that the configured verifier will use."""
    spec = getattr(skill, "spec", skill)
    name = getattr(spec, "name", None)
    contracts = getattr(verifier, "contracts", None)
    if name and isinstance(contracts, dict):
        return contracts.get(str(name))
    return get_contract(skill)


def _recover_write(
    orchestrator: Any,
    case: Any,
    state_before: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attempt adapter compensation and prove it with an independent readback."""
    adapter = getattr(orchestrator, "system_adapter", None) or getattr(orchestrator, "adapter", None)
    restore = getattr(adapter, "restore", None) if adapter is not None else None
    if not callable(restore):
        observed = _snapshot(orchestrator, case)
        return {
            "attempted": False,
            "verified": False,
            "manual_recovery_required": True,
            "reason": "adapter does not expose restore(case_id, state)",
        }, observed

    try:
        restore(str(getattr(case, "id", "")), deepcopy(state_before))
    except Exception as exc:
        observed = _snapshot(orchestrator, case)
        return {
            "attempted": True,
            "verified": False,
            "manual_recovery_required": True,
            "reason": f"restore failed: {type(exc).__name__}: {exc}",
        }, observed

    observed = _snapshot(orchestrator, case)
    verified = observed == state_before
    return {
        "attempted": True,
        "verified": verified,
        "manual_recovery_required": not verified,
        "reason": "readback matched pre-step state" if verified else "restore readback did not match pre-step state",
    }, observed


def _record_step_verdict(
    orchestrator: Any,
    run_id: str,
    case_id: str,
    skill_name: str,
    chain_step: int,
    verdict: StepVerdict,
    *,
    handler_called: bool,
) -> None:
    _record(
        orchestrator,
        run_id,
        case_id,
        "step_verified",
        {
            "skill": skill_name,
            "chain_step": chain_step,
            "handler_called": handler_called,
            "passed": verdict.passed,
            "violations": list(verdict.violations),
            "checks": verdict.checks,
        },
    )


def _decompose(request: str) -> list[str]:
    normalized = request
    for connector in _CONNECTORS:
        normalized = normalized.replace(connector, "|")
    return [part.strip() for part in normalized.split("|") if part.strip()]


def _expand_terms(text: str) -> set[str]:
    terms = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text.replace("_", " "))}
    for source, expanded in _SYNONYMS.items():
        if source in text:
            terms.add(source)
            terms.update(expanded)
    return terms


def _best_skill_for_terms(terms: set[str], skills: list[Any], exclude: set[str]) -> tuple[str, int] | None:
    """Best (skill name, term-hit count) for `terms`, or None when nothing overlaps."""
    scored: list[tuple[int, float, str]] = []
    for skill in skills:
        spec = skill.spec
        if spec.frozen or spec.name in exclude:
            continue
        skill_terms = _expand_terms(" ".join([spec.name, spec.description, *spec.tags]))
        hits = len(terms & skill_terms)
        if hits:
            scored.append((hits, -spec.cost, spec.name))
    if not scored:
        return None
    best = max(scored)
    return best[2], best[0]


def _execute_skill(registry: SkillRegistry, name: str, args: dict[str, Any]):
    """Call the skill with only the arguments its handler accepts."""
    handler = registry.get(name).handler
    signature = inspect.signature(handler)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return registry.execute(name, **args)
    filtered = {key: value for key, value in args.items() if key in signature.parameters}
    return registry.execute(name, **filtered)


def _snapshot(orchestrator: Any, case: Any) -> dict[str, Any]:
    adapter = getattr(orchestrator, "system_adapter", None) or getattr(orchestrator, "adapter", None)
    if adapter is not None and hasattr(adapter, "snapshot"):
        return adapter.snapshot(str(getattr(case, "id", "")))
    return {}


def _record(orchestrator: Any, run_id: str, case_id: str, kind: str, payload: dict) -> None:
    event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
    orchestrator.ledger.append(event)
    orchestrator._run_events.append(event)
