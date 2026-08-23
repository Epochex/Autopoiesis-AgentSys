"""A bounded, pre-registered recovery graph with an auditable state machine.

The graph is a safety boundary, not a planner.  Every action and edge exists at
construction time, the graph must be acyclic, and an incident receives a small
fixed action budget.  A second action is reachable only after the first action
was verifiably reverted and fresh evidence selects a pre-registered edge.

This module deliberately does not execute commands.  Callers execute an
``ActionNode`` through their existing tool contract and report the resulting
precheck, commit, observation, and rollback facts here.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence


RecoveryState = Literal[
    "proposed",
    "prechecked",
    "committed",
    "fast_observing",
    "stability_observing",
    "passed",
    "reverted",
    "escalated",
]

_STATES = frozenset(
    {
        "proposed",
        "prechecked",
        "committed",
        "fast_observing",
        "stability_observing",
        "passed",
        "reverted",
        "escalated",
    }
)
_TERMINAL = frozenset({"passed", "escalated"})
class RecoveryGraphError(ValueError):
    """Base class for invalid recovery graph operations."""


class UnknownActionError(RecoveryGraphError):
    pass


class InvalidGraphError(RecoveryGraphError):
    pass


class InvalidTransitionError(RecoveryGraphError):
    pass


class DuplicateIncidentError(RecoveryGraphError):
    pass


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: Any, *, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name=name)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    return _utc(parsed, name=name)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _strings(value: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _safe_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Round-trip details through canonical JSON to detach caller mutations."""

    if value is None:
        return {}
    normalized = json.loads(_canonical(dict(value)))
    if not isinstance(normalized, dict):  # defensive; input is already Mapping
        raise TypeError("transition details must be an object")
    return normalized


@dataclass(frozen=True, slots=True)
class ActionNode:
    """One pre-approved action in the recovery graph.

    ``impact`` is an ordinal blast-radius level.  Edges may only point to an
    action with an equal or lower value.  ``required_metrics`` are readback
    names that must be present and finite at precheck and both observation
    gates.  Every automatic action must have a verifiable inverse.
    """

    action_id: str
    mechanism: str
    impact: int
    required_metrics: tuple[str, ...]
    rollback_id: str

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("action_id is required")
        if not self.mechanism.strip():
            raise ValueError("mechanism is required")
        if isinstance(self.impact, bool) or self.impact < 0:
            raise ValueError("impact must be a non-negative integer")
        object.__setattr__(self, "required_metrics", _strings(self.required_metrics))
        if not self.required_metrics:
            raise ValueError("an automatic action must declare required metrics")
        if not self.rollback_id.strip():
            raise ValueError("an automatic action must declare a rollback_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "impact": self.impact,
            "mechanism": self.mechanism,
            "required_metrics": list(self.required_metrics),
            "rollback_id": self.rollback_id,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "ActionNode":
        return cls(
            action_id=str(row["action_id"]),
            mechanism=str(row["mechanism"]),
            impact=int(row["impact"]),
            required_metrics=tuple(row.get("required_metrics") or ()),
            rollback_id=str(row["rollback_id"]),
        )


@dataclass(frozen=True, slots=True)
class RecoveryEdge:
    """A pre-approved fallback selected by fresh evidence facts."""

    source_action_id: str
    target_action_id: str
    required_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.source_action_id.strip() or not self.target_action_id.strip():
            raise ValueError("edge endpoints are required")
        object.__setattr__(self, "required_evidence", _strings(self.required_evidence))
        if not self.required_evidence:
            raise ValueError("a recovery edge must require fresh evidence")

    @property
    def edge_id(self) -> str:
        return _id("edge", self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_evidence": list(self.required_evidence),
            "source_action_id": self.source_action_id,
            "target_action_id": self.target_action_id,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "RecoveryEdge":
        return cls(
            source_action_id=str(row["source_action_id"]),
            target_action_id=str(row["target_action_id"]),
            required_evidence=tuple(row.get("required_evidence") or ()),
        )


@dataclass(frozen=True, slots=True)
class FreshEvidence:
    """A bounded fact set observed at a known time."""

    evidence_id: str
    observed_at: datetime
    facts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id is required")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, name="observed_at"))
        object.__setattr__(self, "facts", _strings(self.facts))
        if not self.facts:
            raise ValueError("evidence facts are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "facts": list(self.facts),
            "observed_at": _iso(self.observed_at),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FreshEvidence":
        return cls(
            evidence_id=str(row["evidence_id"]),
            observed_at=_parse_time(row["observed_at"], name="observed_at"),
            facts=tuple(row.get("facts") or ()),
        )


@dataclass(frozen=True, slots=True)
class RecoveryTransition:
    transition_id: str
    sequence: int
    incident_id: str
    action_id: str
    from_state: RecoveryState | None
    to_state: RecoveryState
    at: datetime
    reason: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "at": _iso(self.at),
            "details": _safe_details(self.details),
            "from_state": self.from_state,
            "incident_id": self.incident_id,
            "reason": self.reason,
            "sequence": self.sequence,
            "to_state": self.to_state,
            "transition_id": self.transition_id,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "RecoveryTransition":
        from_state = row.get("from_state")
        if from_state is not None and from_state not in _STATES:
            raise ValueError(f"unsupported transition state: {from_state!r}")
        if row.get("to_state") not in _STATES:
            raise ValueError(f"unsupported transition state: {row.get('to_state')!r}")
        return cls(
            transition_id=str(row["transition_id"]),
            sequence=int(row["sequence"]),
            incident_id=str(row["incident_id"]),
            action_id=str(row["action_id"]),
            from_state=from_state,  # type: ignore[arg-type]
            to_state=str(row["to_state"]),  # type: ignore[arg-type]
            at=_parse_time(row["at"], name="transition.at"),
            reason=str(row["reason"]),
            details=_safe_details(row.get("details")),
        )


@dataclass(slots=True)
class RecoveryRun:
    incident_id: str
    state: RecoveryState
    current_action_id: str
    action_budget: int
    attempted_action_ids: list[str] = field(default_factory=list)
    last_reverted_action_id: str | None = None
    rollback_verified: bool = False
    transitions: list[RecoveryTransition] = field(default_factory=list)

    @property
    def actions_used(self) -> int:
        return len(self.attempted_action_ids)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.action_budget - self.actions_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_budget": self.action_budget,
            "attempted_action_ids": list(self.attempted_action_ids),
            "budget_remaining": self.budget_remaining,
            "current_action_id": self.current_action_id,
            "incident_id": self.incident_id,
            "last_reverted_action_id": self.last_reverted_action_id,
            "rollback_verified": self.rollback_verified,
            "state": self.state,
            "transitions": [transition.to_dict() for transition in self.transitions],
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "RecoveryRun":
        state = str(row["state"])
        if state not in _STATES:
            raise ValueError(f"unsupported recovery state: {state!r}")
        transitions = [
            RecoveryTransition.from_dict(item) for item in row.get("transitions") or ()
        ]
        return cls(
            incident_id=str(row["incident_id"]),
            state=state,  # type: ignore[arg-type]
            current_action_id=str(row["current_action_id"]),
            action_budget=int(row["action_budget"]),
            attempted_action_ids=[str(item) for item in row.get("attempted_action_ids") or ()],
            last_reverted_action_id=(
                str(row["last_reverted_action_id"])
                if row.get("last_reverted_action_id")
                else None
            ),
            rollback_verified=bool(row.get("rollback_verified", False)),
            transitions=transitions,
        )


class RecoveryGraph:
    """Immutable action registry plus bounded per-incident recovery state."""

    SNAPSHOT_VERSION = 1

    def __init__(
        self,
        actions: Iterable[ActionNode],
        edges: Iterable[RecoveryEdge] = (),
        *,
        max_actions_per_incident: int = 2,
        evidence_max_age: timedelta = timedelta(minutes=5),
    ) -> None:
        if isinstance(max_actions_per_incident, bool) or max_actions_per_incident <= 0:
            raise ValueError("max_actions_per_incident must be positive")
        if evidence_max_age <= timedelta(0):
            raise ValueError("evidence_max_age must be positive")
        self.max_actions_per_incident = int(max_actions_per_incident)
        self.evidence_max_age = evidence_max_age
        self._actions: dict[str, ActionNode] = {}
        for action in actions:
            if not isinstance(action, ActionNode):
                raise TypeError("actions must contain ActionNode values")
            if action.action_id in self._actions:
                raise InvalidGraphError(f"duplicate action: {action.action_id}")
            self._actions[action.action_id] = action
        if not self._actions:
            raise InvalidGraphError("at least one action must be registered")

        self._edges: dict[tuple[str, str], RecoveryEdge] = {}
        for edge in edges:
            if not isinstance(edge, RecoveryEdge):
                raise TypeError("edges must contain RecoveryEdge values")
            key = (edge.source_action_id, edge.target_action_id)
            if edge.source_action_id not in self._actions:
                raise UnknownActionError(edge.source_action_id)
            if edge.target_action_id not in self._actions:
                raise UnknownActionError(edge.target_action_id)
            if key in self._edges:
                raise InvalidGraphError(f"duplicate edge: {key!r}")
            source = self._actions[edge.source_action_id]
            target = self._actions[edge.target_action_id]
            if source.mechanism == target.mechanism:
                raise InvalidGraphError(
                    f"edge {edge.edge_id} repeats mechanism {source.mechanism!r}"
                )
            if target.impact > source.impact:
                raise InvalidGraphError(
                    f"edge {edge.edge_id} increases impact from {source.impact} to {target.impact}"
                )
            self._edges[key] = edge
        self._assert_acyclic()
        self._runs: dict[str, RecoveryRun] = {}

    @property
    def actions(self) -> tuple[ActionNode, ...]:
        return tuple(self._actions[key] for key in sorted(self._actions))

    @property
    def edges(self) -> tuple[RecoveryEdge, ...]:
        return tuple(self._edges[key] for key in sorted(self._edges))

    def get_run(self, incident_id: str) -> RecoveryRun | None:
        return self._runs.get(incident_id)

    def _assert_acyclic(self) -> None:
        routes: dict[str, list[str]] = {action_id: [] for action_id in self._actions}
        for source, target in self._edges:
            routes[source].append(target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(action_id: str) -> None:
            if action_id in visiting:
                raise InvalidGraphError("recovery graph must be acyclic")
            if action_id in visited:
                return
            visiting.add(action_id)
            for target in sorted(routes[action_id]):
                visit(target)
            visiting.remove(action_id)
            visited.add(action_id)

        for action_id in sorted(routes):
            visit(action_id)

    def open_incident(
        self,
        incident_id: str,
        action_id: str,
        *,
        at: datetime,
        action_budget: int | None = None,
    ) -> RecoveryRun:
        incident_id = incident_id.strip()
        if not incident_id:
            raise ValueError("incident_id is required")
        self._action(action_id)
        if incident_id in self._runs:
            raise DuplicateIncidentError(incident_id)
        budget = self.max_actions_per_incident if action_budget is None else int(action_budget)
        if budget <= 0 or budget > self.max_actions_per_incident:
            raise ValueError("action_budget must be within the graph limit")
        run = RecoveryRun(
            incident_id=incident_id,
            state="proposed",
            current_action_id=action_id,
            action_budget=budget,
            attempted_action_ids=[action_id],
        )
        self._runs[incident_id] = run
        self._record(
            run,
            from_state=None,
            to_state="proposed",
            at=at,
            reason="registered_action_proposed",
            details={"budget_remaining": run.budget_remaining},
        )
        return run

    def precheck(
        self,
        incident_id: str,
        *,
        at: datetime,
        metrics: Mapping[str, Any] | None,
        management_reachable: bool,
    ) -> RecoveryRun:
        run = self._require_state(incident_id, "proposed")
        if not management_reachable:
            return self._escalate(run, at=at, reason="management_path_unreachable")
        missing = self._missing_metrics(run.current_action_id, metrics)
        if missing:
            return self._escalate(
                run,
                at=at,
                reason="required_metrics_missing",
                details={"missing_metrics": list(missing), "gate": "precheck"},
            )
        return self._move(
            run,
            "prechecked",
            at=at,
            reason="precheck_passed",
            details={"metric_names": sorted(metrics or {})},
        )

    def commit(
        self,
        incident_id: str,
        *,
        at: datetime,
        management_reachable: bool,
        readback_passed: bool,
    ) -> RecoveryRun:
        run = self._require_state(incident_id, "prechecked")
        if not management_reachable:
            return self._escalate(run, at=at, reason="management_path_unreachable")
        if not readback_passed:
            return self._escalate(run, at=at, reason="commit_readback_failed")
        return self._move(run, "committed", at=at, reason="commit_readback_passed")

    def begin_fast_observation(
        self,
        incident_id: str,
        *,
        at: datetime,
        metrics: Mapping[str, Any] | None,
        management_reachable: bool,
    ) -> RecoveryRun:
        run = self._require_state(incident_id, "committed")
        failure = self._observation_gate(
            run,
            at=at,
            metrics=metrics,
            management_reachable=management_reachable,
            gate="fast",
        )
        if failure is not None:
            return failure
        return self._move(
            run,
            "fast_observing",
            at=at,
            reason="fast_observation_opened",
            details={"metric_names": sorted(metrics or {})},
        )

    def begin_stability_observation(
        self,
        incident_id: str,
        *,
        at: datetime,
        metrics: Mapping[str, Any] | None,
        management_reachable: bool,
        fast_window_passed: bool,
    ) -> RecoveryRun:
        run = self._require_state(incident_id, "fast_observing")
        failure = self._observation_gate(
            run,
            at=at,
            metrics=metrics,
            management_reachable=management_reachable,
            gate="fast_completion",
        )
        if failure is not None:
            return failure
        if not fast_window_passed:
            raise InvalidTransitionError(
                "a regressed fast window must be reverted before another action"
            )
        return self._move(
            run,
            "stability_observing",
            at=at,
            reason="fast_observation_passed",
            details={"metric_names": sorted(metrics or {})},
        )

    def pass_stability(
        self,
        incident_id: str,
        *,
        at: datetime,
        metrics: Mapping[str, Any] | None,
        management_reachable: bool,
        stability_window_passed: bool,
    ) -> RecoveryRun:
        run = self._require_state(incident_id, "stability_observing")
        failure = self._observation_gate(
            run,
            at=at,
            metrics=metrics,
            management_reachable=management_reachable,
            gate="stability_completion",
        )
        if failure is not None:
            return failure
        if not stability_window_passed:
            raise InvalidTransitionError(
                "a regressed stability window must be reverted before another action"
            )
        return self._move(
            run,
            "passed",
            at=at,
            reason="stability_observation_passed",
            details={"metric_names": sorted(metrics or {})},
        )

    def revert(
        self,
        incident_id: str,
        *,
        at: datetime,
        rollback_succeeded: bool,
        rollback_metrics: Mapping[str, Any] | None,
        management_reachable: bool,
    ) -> RecoveryRun:
        run = self._run(incident_id)
        if run.state not in {"committed", "fast_observing", "stability_observing"}:
            raise InvalidTransitionError(f"cannot revert from {run.state}")
        if not management_reachable:
            return self._escalate(run, at=at, reason="management_path_unreachable")
        if not rollback_succeeded:
            return self._escalate(run, at=at, reason="rollback_failed")
        missing = self._missing_metrics(run.current_action_id, rollback_metrics)
        if missing:
            return self._escalate(
                run,
                at=at,
                reason="rollback_metrics_missing",
                details={"missing_metrics": list(missing)},
            )
        reverted_action = run.current_action_id
        moved = self._move(
            run,
            "reverted",
            at=at,
            reason="rollback_verified",
            details={
                "metric_names": sorted(rollback_metrics or {}),
                "rollback_id": self._action(reverted_action).rollback_id,
            },
        )
        moved.last_reverted_action_id = reverted_action
        moved.rollback_verified = True
        return moved

    def propose_next(
        self,
        incident_id: str,
        action_id: str,
        *,
        at: datetime,
        evidence: Sequence[FreshEvidence],
    ) -> RecoveryRun:
        """Propose the one pre-registered fallback selected by fresh evidence."""

        target = self._action(action_id)  # unknown actions never enter audit/state
        run = self._require_state(incident_id, "reverted")
        source_id = run.last_reverted_action_id
        if not run.rollback_verified or source_id is None:
            return self._escalate(run, at=at, reason="rollback_not_verified")
        if run.budget_remaining <= 0:
            return self._escalate(run, at=at, reason="action_budget_exhausted")
        edge = self._edges.get((source_id, action_id))
        if edge is None:
            return self._escalate(
                run,
                at=at,
                reason="fallback_edge_not_registered",
                details={"requested_action": action_id, "source_action": source_id},
            )
        source = self._action(source_id)
        if target.impact > source.impact:
            return self._escalate(run, at=at, reason="fallback_impact_increased")
        if target.mechanism == source.mechanism:
            return self._escalate(run, at=at, reason="fallback_mechanism_repeated")

        now = _utc(at, name="at")
        fresh = tuple(
            row
            for row in evidence
            if row.observed_at <= now and now - row.observed_at <= self.evidence_max_age
        )
        facts = {fact for row in fresh for fact in row.facts}
        missing_facts = tuple(sorted(set(edge.required_evidence) - facts))
        if missing_facts:
            return self._escalate(
                run,
                at=now,
                reason="fresh_evidence_not_satisfied",
                details={
                    "edge_id": edge.edge_id,
                    "missing_facts": list(missing_facts),
                },
            )

        previous_state = run.state
        run.current_action_id = action_id
        run.attempted_action_ids.append(action_id)
        run.rollback_verified = False
        self._record(
            run,
            from_state=previous_state,
            to_state="proposed",
            at=now,
            reason="fresh_evidence_selected_registered_fallback",
            details={
                "budget_remaining": run.budget_remaining,
                "edge_id": edge.edge_id,
                "evidence_ids": sorted(row.evidence_id for row in fresh),
                "matched_facts": list(edge.required_evidence),
            },
        )
        run.state = "proposed"
        return run

    def escalate(self, incident_id: str, *, at: datetime, reason: str) -> RecoveryRun:
        """Explicitly stop an active run for an external safety reason."""

        run = self._run(incident_id)
        if run.state in _TERMINAL:
            raise InvalidTransitionError(f"cannot escalate from {run.state}")
        if not reason.strip():
            raise ValueError("escalation reason is required")
        return self._escalate(run, at=at, reason=reason)

    def _action(self, action_id: str) -> ActionNode:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise UnknownActionError(action_id) from exc

    def _run(self, incident_id: str) -> RecoveryRun:
        try:
            return self._runs[incident_id]
        except KeyError as exc:
            raise KeyError(f"unknown incident: {incident_id}") from exc

    def _require_state(self, incident_id: str, state: RecoveryState) -> RecoveryRun:
        run = self._run(incident_id)
        if run.state != state:
            raise InvalidTransitionError(f"expected {state}, found {run.state}")
        return run

    def _missing_metrics(
        self, action_id: str, metrics: Mapping[str, Any] | None
    ) -> tuple[str, ...]:
        values = metrics or {}
        missing: list[str] = []
        for name in self._action(action_id).required_metrics:
            value = values.get(name)
            if value is None or isinstance(value, bool):
                missing.append(name)
                continue
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                missing.append(name)
        return tuple(missing)

    def _observation_gate(
        self,
        run: RecoveryRun,
        *,
        at: datetime,
        metrics: Mapping[str, Any] | None,
        management_reachable: bool,
        gate: str,
    ) -> RecoveryRun | None:
        if not management_reachable:
            return self._escalate(run, at=at, reason="management_path_unreachable")
        missing = self._missing_metrics(run.current_action_id, metrics)
        if missing:
            return self._escalate(
                run,
                at=at,
                reason="required_metrics_missing",
                details={"gate": gate, "missing_metrics": list(missing)},
            )
        return None

    def _move(
        self,
        run: RecoveryRun,
        state: RecoveryState,
        *,
        at: datetime,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> RecoveryRun:
        previous = run.state
        self._record(
            run,
            from_state=previous,
            to_state=state,
            at=at,
            reason=reason,
            details=details,
        )
        run.state = state
        return run

    def _escalate(
        self,
        run: RecoveryRun,
        *,
        at: datetime,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> RecoveryRun:
        return self._move(run, "escalated", at=at, reason=reason, details=details)

    def _record(
        self,
        run: RecoveryRun,
        *,
        from_state: RecoveryState | None,
        to_state: RecoveryState,
        at: datetime,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        timestamp = _utc(at, name="at")
        sequence = len(run.transitions) + 1
        normalized_details = _safe_details(details)
        payload = {
            "action_id": run.current_action_id,
            "at": _iso(timestamp),
            "details": normalized_details,
            "from_state": from_state,
            "incident_id": run.incident_id,
            "reason": reason,
            "sequence": sequence,
            "to_state": to_state,
        }
        run.transitions.append(
            RecoveryTransition(
                transition_id=_id("transition", payload),
                sequence=sequence,
                incident_id=run.incident_id,
                action_id=run.current_action_id,
                from_state=from_state,
                to_state=to_state,
                at=timestamp,
                reason=reason,
                details=normalized_details,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "actions": [self._actions[key].to_dict() for key in sorted(self._actions)],
            "config": {
                "evidence_max_age_seconds": int(self.evidence_max_age.total_seconds()),
                "max_actions_per_incident": self.max_actions_per_incident,
            },
            "edges": [self._edges[key].to_dict() for key in sorted(self._edges)],
            "runs": [self._runs[key].to_dict() for key in sorted(self._runs)],
            "version": self.SNAPSHOT_VERSION,
        }

    def snapshot_json(self) -> str:
        return _canonical(self.snapshot())

    @classmethod
    def restore(cls, raw: Mapping[str, Any] | str) -> "RecoveryGraph":
        if isinstance(raw, str):
            decoded = json.loads(raw)
            if not isinstance(decoded, Mapping):
                raise ValueError("recovery graph snapshot must be an object")
            raw = decoded
        if int(raw.get("version", 0)) != cls.SNAPSHOT_VERSION:
            raise ValueError("unsupported recovery graph snapshot version")
        config = dict(raw.get("config") or {})
        graph = cls(
            actions=(ActionNode.from_dict(row) for row in raw.get("actions") or ()),
            edges=(RecoveryEdge.from_dict(row) for row in raw.get("edges") or ()),
            max_actions_per_incident=int(config["max_actions_per_incident"]),
            evidence_max_age=timedelta(seconds=int(config["evidence_max_age_seconds"])),
        )
        for raw_run in raw.get("runs") or ():
            run = RecoveryRun.from_dict(raw_run)
            if run.incident_id in graph._runs:
                raise ValueError(f"duplicate incident in snapshot: {run.incident_id}")
            if run.current_action_id not in graph._actions:
                raise UnknownActionError(run.current_action_id)
            if not 0 < run.action_budget <= graph.max_actions_per_incident:
                raise ValueError("snapshot action budget exceeds graph limit")
            if len(run.attempted_action_ids) > run.action_budget:
                raise ValueError("snapshot used more actions than its budget")
            if any(action_id not in graph._actions for action_id in run.attempted_action_ids):
                raise UnknownActionError("snapshot contains an unregistered attempted action")
            expected_sequences = list(range(1, len(run.transitions) + 1))
            if [row.sequence for row in run.transitions] != expected_sequences:
                raise ValueError("snapshot transition sequence is not contiguous")
            if any(row.incident_id != run.incident_id for row in run.transitions):
                raise ValueError("snapshot transition belongs to another incident")
            graph._runs[run.incident_id] = run
        return graph


__all__ = [
    "ActionNode",
    "DuplicateIncidentError",
    "FreshEvidence",
    "InvalidGraphError",
    "InvalidTransitionError",
    "RecoveryEdge",
    "RecoveryGraph",
    "RecoveryGraphError",
    "RecoveryRun",
    "RecoveryTransition",
    "UnknownActionError",
]
