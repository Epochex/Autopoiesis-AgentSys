from __future__ import annotations

import inspect
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from core.trace.events import TraceEvent
from core.verifier.contracts import ContractVerifier, StepVerdict


_CONNECTORS = ["通过就", "然后", "再", "并且", "，", ",", ";", " and ", " then "]
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


def plan_skill_chain(request: str, registry) -> list[str]:
    chain: list[str] = []
    for subgoal in _decompose(request):
        terms = _expand_terms(subgoal)
        selected = _best_skill_for_terms(terms, registry.all(), exclude=set(chain))
        if selected is not None:
            chain.append(selected)
    if not chain:
        selected = _best_skill_for_terms(_expand_terms(request), registry.all(), exclude=set())
        if selected is not None:
            chain.append(selected)
    return chain


def execute_chain(chain: list[str], case: Any, orchestrator: Any) -> dict[str, Any]:
    run_id = str(uuid4())
    case_id = str(getattr(case, "id", "chain_case"))
    orchestrator.last_run_id = run_id
    orchestrator._run_events = []
    _record(orchestrator, run_id, case_id, "skill_chain_planned", {"chain": list(chain), "query": getattr(case, "query", "")})

    verifier = getattr(orchestrator, "contract_verifier", ContractVerifier())
    state = _snapshot(orchestrator, case)
    results = []
    verdicts: list[StepVerdict] = []

    for index, name in enumerate(chain, start=1):
        skill = orchestrator.skills.get(name)
        state_before = deepcopy(state)
        args = {"case": case, "state": deepcopy(state_before)}
        result = _execute_skill(orchestrator.skills, name, args)
        state_after = _snapshot(orchestrator, case)
        verdict = verifier.check_step(skill, state_before, args, state_after, result)
        verdicts.append(verdict)
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
        _record(
            orchestrator,
            run_id,
            case_id,
            "step_verified",
            {
                "skill": name,
                "chain_step": index,
                "passed": verdict.passed,
                "violations": list(verdict.violations),
                "checks": verdict.checks,
            },
        )
        state = state_after

    orchestrator._last_evidence = [item for result in results for item in result.evidence]
    return {"chain": list(chain), "results": results, "verdicts": verdicts, "state": state}


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


def _best_skill_for_terms(terms: set[str], skills: list[Any], exclude: set[str]) -> str | None:
    scored = []
    for skill in skills:
        spec = skill.spec
        if spec.frozen or spec.name in exclude:
            continue
        skill_terms = _expand_terms(" ".join([spec.name, spec.description, *spec.tags]))
        hits = len(terms & skill_terms)
        if hits:
            scored.append((hits, -spec.cost, spec.name))
    return sorted(scored, reverse=True)[0][2] if scored else None


def _execute_skill(registry, name: str, args: dict[str, Any]):
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
