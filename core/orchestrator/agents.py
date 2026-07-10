from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.skills.registry import SkillRegistry


TraceRecorder = Callable[[str, str, dict], None]


def _noop_record(case_id: str, kind: str, payload: dict) -> None:
    return None


class PlannerAgent:
    """Propose the next read-only skills for an unresolved case.

    Scores unexecuted, unfrozen read-only skills by overlap with the diagnosis's
    missing evidence (weight 3), the query terms (weight 1), and the case's
    preferred skills (+2); falls back to the cheapest unexecuted skills when
    nothing matches. Emits a `planner_proposed` trace event per proposal.
    """

    MISSING_EVIDENCE_WEIGHT = 3.0
    QUERY_TERM_WEIGHT = 1.0
    PREFERRED_BONUS = 2.0

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        batch_size: int = 2,
        record: TraceRecorder | None = None,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self.registry = registry
        self.batch_size = batch_size
        self._record = record or _noop_record

    def propose(self, case: Any, diagnosis: Any, executed: set[str]) -> list[str]:
        """Return up to `batch_size` skill names to probe next (may be empty).

        `case` needs `id`; `query_terms`/`relevant_skills` and the diagnosis's
        `missing_evidence` are read defensively when present.
        """
        missing_tokens = _tokens(getattr(diagnosis, "missing_evidence", []))
        query_tokens = _tokens(getattr(case, "query_terms", []))
        preferred = set(getattr(case, "relevant_skills", []))

        scored: list[tuple[float, float, str]] = []
        fallback: list[tuple[float, str]] = []
        for skill in self.registry.all():
            spec = skill.spec
            if spec.name in executed or spec.frozen or spec.risk != "read_only":
                continue
            skill_tokens = _tokens([spec.name, spec.description, *spec.tags])
            score = self.MISSING_EVIDENCE_WEIGHT * len(missing_tokens & skill_tokens)
            score += self.QUERY_TERM_WEIGHT * len(query_tokens & skill_tokens)
            if spec.name in preferred:
                score += self.PREFERRED_BONUS
            if score > 0:
                scored.append((score, spec.cost, spec.name))
            else:
                fallback.append((spec.cost, spec.name))

        if scored:
            ordered = [name for _, _, name in sorted(scored, key=lambda item: (-item[0], item[1], item[2]))]
        else:
            ordered = [name for _, name in sorted(fallback)]
        proposed = ordered[: self.batch_size]
        self._record(
            case.id,
            "planner_proposed",
            {
                "skills": proposed,
                "missing_evidence": list(getattr(diagnosis, "missing_evidence", [])),
                "executed": sorted(executed),
            },
        )
        return proposed


class ExecutorAgent:
    """Run proposed read-only skills and append their evidence.

    Enforces the read-only gate at both layers: a non-read-only spec is refused
    before execution, and a handler that *returns* a non-read-only result is
    blocked after the fact. Both raise PermissionError, traced first.
    """

    def __init__(self, *, record: TraceRecorder | None = None):
        self._record = record or _noop_record

    def run(self, case: Any, skill_names: list[str], registry: SkillRegistry) -> tuple[list[dict], float]:
        """Execute `skill_names` in order; returns (collected evidence, total cost).

        Raises KeyError for an unknown skill and PermissionError on any
        read-only violation. Emits `tool_called` + `executor_ran` per skill.
        """
        evidence: list[dict] = []
        total_cost = 0.0
        for name in skill_names:
            skill = registry.get(name)
            if skill.spec.risk != "read_only":
                self._record(
                    case.id,
                    "executor_ran",
                    {
                        "skill": skill.spec.name,
                        "readonly": False,
                        "blocked": True,
                        "reason": "non_readonly_skill",
                    },
                )
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")

            result = registry.execute(skill.spec.name, case=case)
            payload = {
                "skill": skill.spec.name,
                "readonly": result.readonly,
                "evidence_ids": result.evidence_ids(),
                "cost": result.cost,
            }
            if not result.readonly:
                self._record(case.id, "tool_called", {**payload, "blocked": True, "escalation": True})
                self._record(case.id, "executor_ran", {**payload, "blocked": True, "reason": "non_readonly_result"})
                raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")

            total_cost += result.cost
            evidence.extend(result.evidence)
            self._record(case.id, "tool_called", {**payload, "escalation": True})
            self._record(case.id, "executor_ran", {**payload, "blocked": False})
        return evidence, total_cost


class CriticAgent:
    """Rebuild context, re-diagnose, verify, and decide whether escalation is resolved."""

    def __init__(self, *, confidence_threshold: float = 0.6, record: TraceRecorder | None = None):
        self.confidence_threshold = confidence_threshold
        self._record = record or _noop_record

    def review(
        self,
        case: Any,
        evidence: list[dict],
        context_compiler: Any,
        diagnosis_builder: Callable[..., Any],
        verifier: Any,
        memories: dict[str, list],
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Return (diagnosis, report, verdict) for the accumulated evidence.

        The verdict passes only if the verifier passes AND confidence meets the
        threshold; emits `context_compiled`, `verifier_result`,
        `diagnosis_completed`, and `critic_reviewed` trace events.
        """
        context = context_compiler.compile(
            case_id=case.id,
            query=case.query,
            memories_by_tier=memories,
            current_evidence=evidence,
            required_evidence=[],
        )
        self._record(case.id, "context_compiled", context.model_dump(mode="json"))

        diagnosis = diagnosis_builder(case=case, evidence=evidence, context=context)
        report = verifier.verify(diagnosis, evidence, [])
        self._record(case.id, "verifier_result", report.model_dump(mode="json"))
        self._record(case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))

        confidence = float(getattr(diagnosis, "confidence", 0.0))
        passed = bool(getattr(report, "passed", False)) and confidence >= self.confidence_threshold
        verdict = {
            "passed": passed,
            "continue": not passed,
            "verifier_passed": bool(getattr(report, "passed", False)),
            "confidence": confidence,
            "confidence_threshold": self.confidence_threshold,
        }
        self._record(case.id, "critic_reviewed", verdict)
        return diagnosis, report, verdict


def _tokens(values: Any) -> set[str]:
    """Lower-cased alphanumeric tokens from a string or iterable of stringables."""
    if isinstance(values, str):
        values = [values]
    return {
        token.lower()
        for value in values or []
        for token in re.findall(r"[A-Za-z0-9]+", str(value).replace("_", " "))
    }
