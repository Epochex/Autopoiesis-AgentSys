from __future__ import annotations

from core.orchestrator.agents import CriticAgent, ExecutorAgent, PlannerAgent
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
        self.planner = PlannerAgent(
            self.skills,
            batch_size=self.planner_batch_size,
            record=self._record_agent_event,
        )
        self.executor = ExecutorAgent(record=self._record_agent_event)
        self.critic = CriticAgent(
            confidence_threshold=self.confidence_threshold,
            record=self._record_agent_event,
        )

    def diagnose(self, case) -> tuple[object, object]:
        self.planner.batch_size = max(1, self.planner_batch_size)
        self.critic.confidence_threshold = self.confidence_threshold

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
            proposed = self.planner.propose(case, diagnosis, executed)
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
            added, cost = self.executor.run(case, proposed, self.skills)
            evidence.extend(added)
            executed.update(proposed)

            diagnosis, report, verdict = self.critic.review(
                case,
                evidence,
                self.context_compiler,
                self.diagnosis_builder,
                self.verifier,
                memories,
            )
            self._record(
                run_id,
                case.id,
                "cost_observed",
                {"tool_cost": cost, "tool_calls": len(proposed), "escalation_round": round_number},
            )

            if verdict["passed"]:
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

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
        self.base.ledger.append(event)
        self.base._run_events.append(event)
        self._sync_from_base()

    def _record_agent_event(self, case_id: str, kind: str, payload: dict) -> None:
        self._record(self.base.last_run_id, case_id, kind, payload)

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
