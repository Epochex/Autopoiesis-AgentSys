from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from core.orchestrator.agents import (
    CriticAgent,
    ExecutorAgent,
    ParallelExecutorAgent,
    PlannerAgent,
)
from core.orchestrator.orchestrator import CaseLike, SingleAgentRCAOrchestrator
from core.trace.events import TraceEvent
from core.verifier.verifier import VerificationReport


def has_high_blast_radius(case: CaseLike) -> bool:
    """True when the case declares a high blast radius — a routing/escalation feature."""
    if bool(getattr(case, "high_blast", False)):
        return True
    blast_radius = str(getattr(case, "blast_radius", "") or "").strip().lower()
    return blast_radius in {"high", "critical", "wide", "large", "global"}


def has_high_complexity(case: CaseLike, *, threshold: float = 0.7) -> bool:
    """Return only explicit or structurally strong complexity signals."""
    if bool(getattr(case, "complex", False)) or bool(getattr(case, "compound", False)):
        return True
    declared = str(getattr(case, "complexity", "") or "").strip().lower()
    if declared in {"high", "critical", "complex", "compound"}:
        return True
    try:
        if float(getattr(case, "complexity_score", 0.0) or 0.0) >= threshold:
            return True
    except (TypeError, ValueError):
        pass
    # Multiple assets and a broad diagnostic vocabulary are a domain-neutral
    # structural signal; either feature alone is too weak to force escalation.
    return len(getattr(case, "assets", []) or []) >= 3 and len(
        getattr(case, "query_terms", []) or []
    ) >= 6


@dataclass(frozen=True)
class ResourceSnapshot:
    """Normalized local or externally supplied resource pressure."""

    cpu: float = 0.0
    memory: float = 0.0
    pending_tasks: int = 0
    source: str = "local"

    def __post_init__(self) -> None:
        if not 0.0 <= self.cpu <= 1.0:
            raise ValueError(f"cpu pressure must be in [0, 1], got {self.cpu}")
        if not 0.0 <= self.memory <= 1.0:
            raise ValueError(f"memory pressure must be in [0, 1], got {self.memory}")
        if self.pending_tasks < 0:
            raise ValueError(f"pending_tasks must be >= 0, got {self.pending_tasks}")

    @property
    def pressure(self) -> float:
        return max(self.cpu, self.memory)

    def as_payload(self) -> dict[str, Any]:
        return {
            "cpu": round(self.cpu, 4),
            "memory": round(self.memory, 4),
            "pending_tasks": self.pending_tasks,
            "source": self.source,
        }


def read_local_resource_snapshot() -> ResourceSnapshot:
    """Read dependency-free host pressure; callers may inject cluster metrics instead."""
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        cpu = min(1.0, max(0.0, os.getloadavg()[0] / cpu_count))
    except (AttributeError, OSError):
        cpu = 0.0

    memory = 0.0
    try:
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            fields[key] = int(value.strip().split()[0])
        total = fields.get("MemTotal", 0)
        available = fields.get("MemAvailable", total)
        if total > 0:
            memory = min(1.0, max(0.0, 1.0 - (available / total)))
    except (OSError, ValueError, IndexError):
        pass
    return ResourceSnapshot(cpu=cpu, memory=memory, source="local_procfs")


class ResourceAwareConcurrency:
    """Convert pressure watermarks into a deterministic specialist concurrency cap."""

    def __init__(
        self,
        max_parallel_agents: int,
        *,
        high_watermark: float = 0.75,
        critical_watermark: float = 0.9,
    ):
        if max_parallel_agents < 1:
            raise ValueError("max_parallel_agents must be >= 1")
        if not 0.0 <= high_watermark < critical_watermark <= 1.0:
            raise ValueError("resource watermarks must satisfy 0 <= high < critical <= 1")
        self.max_parallel_agents = max_parallel_agents
        self.high_watermark = high_watermark
        self.critical_watermark = critical_watermark

    def limit(self, snapshot: ResourceSnapshot) -> int:
        if snapshot.pressure >= self.critical_watermark:
            slots = 1
        elif snapshot.pressure >= self.high_watermark:
            slots = max(1, self.max_parallel_agents // 2)
        else:
            slots = self.max_parallel_agents
        if snapshot.pending_tasks:
            slots = max(1, slots - min(slots - 1, snapshot.pending_tasks))
        return slots


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
        planner_batch_size: int = 4,
        max_parallel_agents: int = 4,
        resource_probe: Callable[[], ResourceSnapshot] | None = None,
        high_resource_watermark: float = 0.75,
        critical_resource_watermark: float = 0.9,
        reject_on_insufficient_evidence: bool = True,
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
        self.max_parallel_agents = max_parallel_agents
        self.resource_probe = resource_probe or read_local_resource_snapshot
        self.reject_on_insufficient_evidence = reject_on_insufficient_evidence
        self.concurrency = ResourceAwareConcurrency(
            max_parallel_agents,
            high_watermark=high_resource_watermark,
            critical_watermark=critical_resource_watermark,
        )

        self.memory = base_orchestrator.memory
        self.context_compiler = base_orchestrator.context_compiler
        self.skills = base_orchestrator.skills
        self.skill_controller = base_orchestrator.skill_controller
        self.verifier = base_orchestrator.verifier
        self.diagnosis_builder = base_orchestrator.diagnosis_builder
        self.ledger = base_orchestrator.ledger
        self.observer = base_orchestrator.observer
        self.planner = PlannerAgent(
            self.skills,
            batch_size=self.planner_batch_size,
            record=self._record_agent_event,
        )
        self.executor = ExecutorAgent(record=self._record_agent_event)
        self.parallel_executor = ParallelExecutorAgent(record=self._record_agent_event)
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

    def diagnose(
        self,
        case: CaseLike,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        observe_root: bool = True,
    ) -> tuple[Any, VerificationReport]:
        """Diagnose `case`; escalate to planner/executor/critic rounds while unresolved.

        Returns (diagnosis, report) from the last completed review. Raises
        PermissionError if any (base or escalation) step touches a non-read-only skill.
        """
        # propagate post-construction tuning of the public knobs to the agents
        self.planner.batch_size = self.planner_batch_size
        self.critic.confidence_threshold = self.confidence_threshold

        diagnosis, report = self.base.diagnose(
            case,
            run_id=run_id,
            session_id=session_id,
            observe_root=observe_root,
        )

        reasons = self._escalation_reasons(case, diagnosis, report)
        if not reasons:
            return diagnosis, report

        run_id = self.base.last_run_id
        memories = self.memory.retrieve(case.query_terms, case.assets)
        evidence = [dict(item) for item in self.base._last_evidence]
        executed = self._executed_skill_names()
        rounds_used = 0
        final_verdict: dict[str, Any] = {
            "passed": bool(getattr(report, "passed", False)),
            "insufficient_evidence": bool(getattr(diagnosis, "missing_evidence", [])),
            "conflicts": [],
        }

        for round_number in range(1, self.max_rounds + 1):
            snapshot = self.resource_probe()
            if not isinstance(snapshot, ResourceSnapshot):
                snapshot = ResourceSnapshot(**dict(snapshot))
            concurrency_limit = self.concurrency.limit(snapshot)
            assignments = self.planner.plan_parallel(
                case,
                diagnosis,
                executed,
                max_roles=concurrency_limit,
            )
            proposed = [skill for assignment in assignments for skill in assignment.skill_names]
            self._record(
                run_id,
                case.id,
                "topology_escalated",
                {
                    "reason": ", ".join(reasons),
                    "round": round_number,
                    "planner_proposed_skills": proposed,
                    "specialist_roles": [assignment.role for assignment in assignments],
                    "resource_snapshot": snapshot.as_payload(),
                    "parallel_limit": concurrency_limit,
                },
            )
            if not proposed:
                break

            rounds_used = round_number
            added, cost, role_findings = self.parallel_executor.run(
                case,
                assignments,
                self.skills,
                max_workers=concurrency_limit,
            )
            evidence.extend(added)
            executed.update(proposed)

            diagnosis, report, final_verdict = self.critic.review(
                case,
                evidence,
                self.context_compiler,
                self.diagnosis_builder,
                self.verifier,
                memories,
                role_findings,
            )
            evidence, _ = self.critic._merge_role_evidence(evidence, role_findings)
            self._record(
                run_id,
                case.id,
                "cost_observed",
                {"tool_cost": cost, "tool_calls": len(proposed), "escalation_round": round_number},
            )

            if final_verdict["passed"]:
                break
            reasons = self._escalation_reasons(case, diagnosis, report) or ["unresolved_after_escalation_round"]

        unresolved = (
            not bool(getattr(report, "passed", False))
            or float(getattr(diagnosis, "confidence", 0.0)) < self.confidence_threshold
            or bool(getattr(diagnosis, "missing_evidence", []))
            or bool(final_verdict.get("conflicts"))
            or bool(final_verdict.get("insufficient_evidence"))
        )
        rejected = False
        if unresolved and self.reject_on_insufficient_evidence:
            diagnosis, report = self._reject_diagnosis(diagnosis, report)
            rejected = True
            self._record(
                run_id,
                case.id,
                "critic_reviewed",
                {
                    "passed": False,
                    "continue": False,
                    "rejected": True,
                    "reason": "insufficient_or_conflicting_evidence",
                    "conflicts": final_verdict.get("conflicts", []),
                },
            )
            # The abstention must be the terminal verifier/diagnosis pair in the
            # append-only trace; downstream replay and memory consolidation read
            # the last diagnosis, so leaving the rejected claim there would be a
            # fail-open learning bug.
            self._record(run_id, case.id, "verifier_result", report.model_dump(mode="json"))
            self._record(run_id, case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))

        self.base._last_evidence = evidence
        self._record(
            run_id,
            case.id,
            "escalation_resolved",
            {
                "rounds_used": rounds_used,
                "final_confidence": float(getattr(diagnosis, "confidence", 0.0)),
                "passed": bool(getattr(report, "passed", False)),
                "rejected": rejected,
                "specialist_parallelism": self.max_parallel_agents,
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
        if self._has_high_complexity(case):
            reasons.append("high_complexity")
        return reasons

    @staticmethod
    def _has_high_blast_radius(case: CaseLike) -> bool:
        return has_high_blast_radius(case)

    @staticmethod
    def _has_high_complexity(case: CaseLike) -> bool:
        return has_high_complexity(case)

    @staticmethod
    def _reject_diagnosis(diagnosis: Any, report: VerificationReport) -> tuple[Any, VerificationReport]:
        """Return an explicit abstention without assuming a domain-specific model class."""
        missing = list(dict.fromkeys([
            *list(getattr(diagnosis, "missing_evidence", []) or []),
            "additional_current_evidence_required",
        ]))
        updates = {
            "root_cause_key": "unknown",
            "root_cause": "Insufficient or conflicting current evidence; diagnosis refused.",
            "confidence": 0.0,
            "evidence": [],
            "missing_evidence": missing,
            "recommended_actions": ["Collect additional read-only evidence and retry."],
            "readonly": True,
        }
        if hasattr(diagnosis, "model_copy"):
            allowed = set(getattr(type(diagnosis), "model_fields", {}))
            diagnosis = diagnosis.model_copy(update={key: value for key, value in updates.items() if key in allowed})
        errors = list(dict.fromkeys([
            *list(getattr(report, "errors", []) or []),
            "adaptive reviewer refused an ungrounded diagnosis",
        ]))
        return diagnosis, VerificationReport(
            passed=False,
            errors=errors,
            evidence_recall=float(getattr(report, "evidence_recall", 0.0)),
        )

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
    planner_batch_size: int = 4,
    max_parallel_agents: int = 4,
    resource_probe: Callable[[], ResourceSnapshot] | None = None,
    high_resource_watermark: float = 0.75,
    critical_resource_watermark: float = 0.9,
    reject_on_insufficient_evidence: bool = True,
) -> AdaptiveOrchestrator:
    """Wrap a single-agent orchestrator with bounded multi-agent escalation."""
    return AdaptiveOrchestrator(
        base_orchestrator,
        confidence_threshold=confidence_threshold,
        max_rounds=max_rounds,
        planner_batch_size=planner_batch_size,
        max_parallel_agents=max_parallel_agents,
        resource_probe=resource_probe,
        high_resource_watermark=high_resource_watermark,
        critical_resource_watermark=critical_resource_watermark,
        reject_on_insufficient_evidence=reject_on_insufficient_evidence,
    )
