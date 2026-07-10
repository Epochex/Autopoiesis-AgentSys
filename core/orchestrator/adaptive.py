from __future__ import annotations

from typing import Any

from core.orchestrator.agents import CriticAgent, ExecutorAgent, PlannerAgent
from core.orchestrator.orchestrator import CaseLike, SingleAgentRCAOrchestrator
from core.trace.events import TraceEvent
from core.verifier.verifier import VerificationReport


def has_high_blast_radius(case: CaseLike) -> bool:
    """True when the case declares a high blast radius — a routing/escalation feature."""
    if bool(getattr(case, "high_blast", False)):
        return True
    blast_radius = str(getattr(case, "blast_radius", "") or "").strip().lower()
    return blast_radius in {"high", "critical", "wide", "large", "global"}


class AdaptiveOrchestrator:
    """Single-agent first, with bounded read-only escalation when ambiguity remains.

    Escalation runs an explicit planner -> executor -> critic round (each agent
    emits its own trace event) for at most `max_rounds` rounds; the executor
    enforces the same read-only gate as the base path. Run state (events,
    evidence, run id) lives on the wrapped base orchestrator, so this wrapper is
    a drop-in replacement wherever the single-agent orchestrator is accepted.
    """

    def __init__(
        self,
        base_orchestrator: SingleAgentRCAOrchestrator,
        *,
        confidence_threshold: float = 0.6,
        max_rounds: int = 2,
        planner_batch_size: int = 2,
    ):
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold}")
        if max_rounds < 0:
            raise ValueError(f"max_rounds must be >= 0, got {max_rounds}")
        if planner_batch_size < 1:
            raise ValueError(f"planner_batch_size must be >= 1, got {planner_batch_size}")
        self.base = base_orchestrator
        self.confidence_threshold = confidence_threshold
        self.max_rounds = max_rounds
        self.planner_batch_size = planner_batch_size

        self.memory = base_orchestrator.memory
        self.context_compiler = base_orchestrator.context_compiler
        self.skills = base_orchestrator.skills
        self.skill_controller = base_orchestrator.skill_controller
        self.verifier = base_orchestrator.verifier
        self.diagnosis_builder = base_orchestrator.diagnosis_builder
        self.ledger = base_orchestrator.ledger
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

    # Run state is owned by the base orchestrator; delegate instead of mirroring
    # so the two views can never drift apart mid-run.
    @property
    def _run_events(self) -> list[TraceEvent]:
        return self.base._run_events

    @_run_events.setter
    def _run_events(self, value: list[TraceEvent]) -> None:
        self.base._run_events = value

    @property
    def _last_evidence(self) -> list[dict]:
        return self.base._last_evidence

    @_last_evidence.setter
    def _last_evidence(self, value: list[dict]) -> None:
        self.base._last_evidence = value

    @property
    def last_run_id(self) -> str:
        return self.base.last_run_id

    @last_run_id.setter
    def last_run_id(self, value: str) -> None:
        self.base.last_run_id = value

    def diagnose(self, case: CaseLike) -> tuple[Any, VerificationReport]:
        """Diagnose `case`; escalate to planner/executor/critic rounds while unresolved.

        Returns (diagnosis, report) from the last completed review. Raises
        PermissionError if any (base or escalation) step touches a non-read-only skill.
        """
        # propagate post-construction tuning of the public knobs to the agents
        self.planner.batch_size = self.planner_batch_size
        self.critic.confidence_threshold = self.confidence_threshold

        diagnosis, report = self.base.diagnose(case)

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

    def _escalation_reasons(self, case: CaseLike, diagnosis: Any, report: Any) -> list[str]:
        """Why this result cannot stand as-is; empty list means no escalation."""
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
    def _has_high_blast_radius(case: CaseLike) -> bool:
        return has_high_blast_radius(case)

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

    def _record_agent_event(self, case_id: str, kind: str, payload: dict) -> None:
        self._record(self.base.last_run_id, case_id, kind, payload)


def build_adaptive_orchestrator(
    base_orchestrator: SingleAgentRCAOrchestrator,
    *,
    confidence_threshold: float = 0.6,
    max_rounds: int = 2,
    planner_batch_size: int = 2,
) -> AdaptiveOrchestrator:
    """Wrap a single-agent orchestrator with bounded multi-agent escalation."""
    return AdaptiveOrchestrator(
        base_orchestrator,
        confidence_threshold=confidence_threshold,
        max_rounds=max_rounds,
        planner_batch_size=planner_batch_size,
    )
