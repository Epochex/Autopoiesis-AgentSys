from __future__ import annotations

import re
from typing import Any

from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.trace.events import TraceEvent


class AdaptiveOrchestrator:
    """Single-agent first, with bounded read-only escalation when ambiguity remains."""

    def __init__(
        self,
        base_orchestrator: SingleAgentRCAOrchestrator,
        *,
        confidence_threshold: float = 0.6,
        max_rounds: int = 2,
        planner_batch_size: int = 2,
    ):
        self.base = base_orchestrator
        self.confidence_threshold = confidence_threshold
        self.max_rounds = max(0, max_rounds)
        self.planner_batch_size = max(1, planner_batch_size)

        self.memory = base_orchestrator.memory
        self.context_compiler = base_orchestrator.context_compiler
        self.skills = base_orchestrator.skills
        self.skill_controller = base_orchestrator.skill_controller
        self.verifier = base_orchestrator.verifier
        self.diagnosis_builder = base_orchestrator.diagnosis_builder
        self.ledger = base_orchestrator.ledger
        self._run_events = base_orchestrator._run_events
        self._last_evidence = base_orchestrator._last_evidence
        self.last_run_id = base_orchestrator.last_run_id

    def diagnose(self, case) -> tuple[object, object]:
        diagnosis, report = self.base.diagnose(case)
        self._sync_from_base()

        reasons = self._escalation_reasons(case, diagnosis, report)
        if not reasons:
            return diagnosis, report

        run_id = self.base.last_run_id
        memories = self.memory.retrieve(case.query_terms, case.assets)
        evidence = [dict(item) for item in self.base._last_evidence]
        executed = self._executed_skill_names()
        rounds_used = 0

        for round_number in range(1, self.max_rounds + 1):
            proposed = self._plan_next_skills(case, diagnosis, executed)
            self._record(
                run_id,
                case.id,
                "topology_escalated",
                {
                    "reason": ", ".join(reasons),
                    "round": round_number,
                    "planner_proposed_skills": proposed,
                },
            )
            if not proposed:
                break

            rounds_used = round_number
            added, cost = self._execute_readonly_skills(case, proposed, run_id)
            evidence.extend(added)
            executed.update(proposed)

            context = self.context_compiler.compile(
                case_id=case.id,
                query=case.query,
                memories_by_tier=memories,
                current_evidence=evidence,
                required_evidence=[],
            )
            self._record(run_id, case.id, "context_compiled", context.model_dump(mode="json"))

            diagnosis = self.diagnosis_builder(case=case, evidence=evidence, context=context)
            report = self.verifier.verify(diagnosis, evidence, [])
            self._record(run_id, case.id, "verifier_result", report.model_dump(mode="json"))
            self._record(
                run_id,
                case.id,
                "cost_observed",
                {"tool_cost": cost, "tool_calls": len(proposed), "escalation_round": round_number},
            )
            self._record(run_id, case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))

            if report.passed and float(getattr(diagnosis, "confidence", 0.0)) >= self.confidence_threshold:
                break
            reasons = self._escalation_reasons(case, diagnosis, report) or ["unresolved_after_escalation_round"]

        self.base._last_evidence = evidence
        self._sync_from_base()
        self._record(
            run_id,
            case.id,
            "escalation_resolved",
            {
                "rounds_used": rounds_used,
                "final_confidence": float(getattr(diagnosis, "confidence", 0.0)),
                "passed": bool(getattr(report, "passed", False)),
            },
        )
        return diagnosis, report

    def _execute_readonly_skills(self, case, skill_names: list[str], run_id: str) -> tuple[list[dict], float]:
        evidence: list[dict] = []
        total_cost = 0.0
        for name in skill_names:
            skill = self.skills.get(name)
            if skill.spec.risk != "read_only":
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")
            result = self.skills.execute(skill.spec.name, case=case)
            if not result.readonly:
                self._record(
                    run_id,
                    case.id,
                    "tool_called",
                    {
                        "skill": skill.spec.name,
                        "readonly": result.readonly,
                        "evidence_ids": [item["evidence_id"] for item in result.evidence],
                        "cost": result.cost,
                        "blocked": True,
                        "escalation": True,
                    },
                )
                raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")
            total_cost += result.cost
            evidence.extend(result.evidence)
            self._record(
                run_id,
                case.id,
                "tool_called",
                {
                    "skill": skill.spec.name,
                    "readonly": result.readonly,
                    "evidence_ids": [item["evidence_id"] for item in result.evidence],
                    "cost": result.cost,
                    "escalation": True,
                },
            )
        return evidence, total_cost

    def _plan_next_skills(self, case, diagnosis, executed: set[str]) -> list[str]:
        missing_tokens = self._tokens(getattr(diagnosis, "missing_evidence", []))
        query_tokens = self._tokens(getattr(case, "query_terms", []))
        preferred = set(getattr(case, "relevant_skills", []))

        scored: list[tuple[float, float, str]] = []
        fallback: list[tuple[float, str]] = []
        for skill in self.skills.all():
            spec = skill.spec
            if spec.name in executed or spec.frozen or spec.risk != "read_only":
                continue
            skill_tokens = self._tokens([spec.name, spec.description, *spec.tags])
            score = 0.0
            score += 3.0 * len(missing_tokens & skill_tokens)
            score += 1.0 * len(query_tokens & skill_tokens)
            if spec.name in preferred:
                score += 2.0
            if score > 0:
                scored.append((score, spec.cost, spec.name))
            else:
                fallback.append((spec.cost, spec.name))

        if scored:
            ordered = [name for _, _, name in sorted(scored, key=lambda item: (-item[0], item[1], item[2]))]
        else:
            ordered = [name for _, name in sorted(fallback, key=lambda item: (item[0], item[1]))]
        return ordered[: self.planner_batch_size]

    def _escalation_reasons(self, case, diagnosis, report) -> list[str]:
        reasons: list[str] = []
        if not getattr(report, "passed", False):
            reasons.append("report_failed")
        if float(getattr(diagnosis, "confidence", 0.0)) < self.confidence_threshold:
            reasons.append("low_confidence")
        if getattr(diagnosis, "missing_evidence", []):
            reasons.append("missing_evidence")
        if self._has_high_blast_radius(case):
            reasons.append("high_blast_radius")
        return reasons

    @staticmethod
    def _has_high_blast_radius(case) -> bool:
        if bool(getattr(case, "high_blast", False)):
            return True
        blast_radius = str(getattr(case, "blast_radius", "") or "").strip().lower()
        return blast_radius in {"high", "critical", "wide", "large", "global"}

    def _executed_skill_names(self) -> set[str]:
        return {
            str(event.payload.get("skill"))
            for event in self.base._run_events
            if event.kind == "tool_called" and event.payload.get("skill") and not event.payload.get("blocked")
        }

    @staticmethod
    def _tokens(values: Any) -> set[str]:
        if isinstance(values, str):
            values = [values]
        return {
            token.lower()
            for value in values or []
            for token in re.findall(r"[A-Za-z0-9]+", str(value).replace("_", " "))
        }

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
        self.base.ledger.append(event)
        self.base._run_events.append(event)
        self._sync_from_base()

    def _sync_from_base(self) -> None:
        self._run_events = self.base._run_events
        self._last_evidence = self.base._last_evidence
        self.last_run_id = self.base.last_run_id


def build_adaptive_orchestrator(
    base_orchestrator: SingleAgentRCAOrchestrator,
    *,
    confidence_threshold: float = 0.6,
    max_rounds: int = 2,
) -> AdaptiveOrchestrator:
    return AdaptiveOrchestrator(
        base_orchestrator,
        confidence_threshold=confidence_threshold,
        max_rounds=max_rounds,
    )
