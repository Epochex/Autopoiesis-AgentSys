from __future__ import annotations

import inspect
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from core.verifier.contracts import ContractVerifier, StepVerdict


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
    Each step emits `tool_called` + `step_verified` trace events; failing
    contracts are recorded, not raised, so the caller sees the exact step.

    Returns {"chain", "results", "verdicts", "state"}. Raises KeyError for an
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

    orchestrator._last_evidence = [item for step_result in results for item in step_result.evidence]
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
