"""Read-only shadow analysis of saved investigation activity trajectories."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


_ACTIVITY_KEYS = ("kind", "event_kind", "eventKind", "type", "name", "action")
_TRACE_KEYS = ("trace_events", "traceEvents", "events", "activity_log")
_TERMINAL_FAILURE_ACTIVITIES = {
    "bakein_regressed",
    "case_history_write_failed",
    "remediation_reverted",
    "revert_unverified",
    "step_rolled_back",
    "tool_failed",
}
_ADVERSE_DECISION_STATES = {"escalated", "failed", "investigating"}
_ADVERSE_CLASSIFICATION_TOKENS = ("data_gap", "exhausted", "unresolved")
_MIN_RISK_SAMPLES = 20
_TRACE_EVENT_LIMIT_DEFAULT = 500


@dataclass
class _TrieNode:
    visits: int = 0
    terminal: int = 0
    children: dict[str, "_TrieNode"] = field(default_factory=dict)


def _normalise_activity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text).casefold()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:96] or None


def _events(snapshot: Mapping[str, Any]) -> Sequence[Any]:
    for key in _TRACE_KEYS:
        value = snapshot.get(key)
        if isinstance(value, (list, tuple)):
            return value
    return ()


def _bounded_snapshot(
    snapshot: Mapping[str, Any], max_events_per_session: int
) -> tuple[dict[str, Any], bool]:
    bounded = dict(snapshot)
    for key in _TRACE_KEYS:
        events = snapshot.get(key)
        if not isinstance(events, (list, tuple)):
            continue
        if len(events) <= max_events_per_session:
            return bounded, False
        # The trailing window preserves the current state and never invents an
        # edge between non-adjacent events.
        bounded[key] = list(events[-max_events_per_session:])
        return bounded, True
    return bounded, False


def extract_activity_sequence(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract a deterministic activity sequence from current and legacy snapshots."""
    activities: list[str] = []
    for event in _events(snapshot):
        raw: Any = event
        if isinstance(event, Mapping):
            raw = next(
                (event.get(key) for key in _ACTIVITY_KEYS if event.get(key) is not None),
                None,
            )
            if raw is None and isinstance(event.get("payload"), Mapping):
                payload = event["payload"]
                raw = next(
                    (
                        payload.get(key)
                        for key in _ACTIVITY_KEYS
                        if payload.get(key) is not None
                    ),
                    None,
                )
        activity = _normalise_activity(raw)
        if activity is not None:
            activities.append(activity)
    return tuple(activities)


def _session_id(snapshot: Mapping[str, Any]) -> str:
    return str(snapshot.get("session_id") or snapshot.get("sessionId") or "").strip()


def _case_id(snapshot: Mapping[str, Any]) -> str:
    return str(snapshot.get("case_id") or snapshot.get("caseId") or "").strip()


def _context(prefix: Sequence[str], context_window: int) -> tuple[str, ...]:
    return tuple(prefix[-context_window:]) if prefix else ()


def _state_id(context: Sequence[str]) -> str:
    return "start" if not context else "state:" + "|".join(context)


def _state_for(prefix: Sequence[str], context_window: int) -> str:
    return _state_id(_context(prefix, context_window))


def _build_trie(sequences: Iterable[Sequence[str]]) -> _TrieNode:
    root = _TrieNode()
    for sequence in sequences:
        root.visits += 1
        node = root
        for activity in sequence:
            node = node.children.setdefault(activity, _TrieNode())
            node.visits += 1
        node.terminal += 1
    return root


def _failed_outcome(snapshot: Mapping[str, Any], sequence: Sequence[str]) -> bool:
    if any(activity in _TERMINAL_FAILURE_ACTIVITIES for activity in sequence):
        return True
    decision = snapshot.get("decision")
    if isinstance(decision, Mapping):
        state = str(decision.get("state") or decision.get("status") or "").casefold()
        if state in _ADVERSE_DECISION_STATES:
            return True
        classification = str(decision.get("classification") or "").casefold()
        if any(token in classification for token in _ADVERSE_CLASSIFICATION_TOKENS):
            return True
    resolution = str(snapshot.get("resolution") or "").casefold()
    return resolution == "inconclusive"


def _cycle_counts(sequence: Sequence[str], context_window: int) -> tuple[int, int]:
    prefix: list[str] = []
    visited = {"start"}
    repeated = 0
    for activity in sequence:
        prefix.append(activity)
        state = _state_for(prefix, context_window)
        repeated += int(state in visited)
        visited.add(state)
    return repeated, len(sequence)


def build_trajectory_model(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    context_window: int = 2,
) -> dict[str, Any]:
    """Build a prefix trie and merge prefixes by bounded trailing context."""
    if not 1 <= int(context_window) <= 4:
        raise ValueError("context_window must be between 1 and 4")
    context_window = int(context_window)
    rows = [snapshot for snapshot in snapshots if isinstance(snapshot, Mapping)]
    sequences = [extract_activity_sequence(snapshot) for snapshot in rows]
    trie = _build_trie(sequences)

    state_prefix_visits: defaultdict[str, int] = defaultdict(int)
    state_terminal_visits: defaultdict[str, int] = defaultdict(int)
    transition_counts: defaultdict[tuple[str, str, str], int] = defaultdict(int)

    def collect(node: _TrieNode, prefix: tuple[str, ...]) -> None:
        source = _state_for(prefix, context_window)
        state_prefix_visits[source] += node.visits
        state_terminal_visits[source] += node.terminal
        for activity in sorted(node.children):
            child = node.children[activity]
            target = _state_for((*prefix, activity), context_window)
            transition_counts[(source, activity, target)] += child.visits
            collect(child, (*prefix, activity))

    collect(trie, ())

    state_sessions: defaultdict[str, int] = defaultdict(int)
    state_failures: defaultdict[str, int] = defaultdict(int)
    total_cycle_edges = 0
    total_edges = 0
    for snapshot, sequence in zip(rows, sequences):
        prefix: list[str] = []
        reached = {"start"}
        for activity in sequence:
            prefix.append(activity)
            reached.add(_state_for(prefix, context_window))
        failed = _failed_outcome(snapshot, sequence)
        for state in reached:
            state_sessions[state] += 1
            state_failures[state] += int(failed)
        cycle_edges, edges = _cycle_counts(sequence, context_window)
        total_cycle_edges += cycle_edges
        total_edges += edges

    outgoing: defaultdict[str, int] = defaultdict(int)
    for (source, _activity, _target), count in transition_counts.items():
        outgoing[source] += count

    state_ids = sorted(state_prefix_visits)
    states: list[dict[str, Any]] = []
    for state in state_ids:
        state_context = [] if state == "start" else state.removeprefix("state:").split("|")
        samples = state_sessions[state]
        failures = state_failures[state]
        states.append({
            "state_id": state,
            "last_activity": state_context[-1] if state_context else None,
            "context": state_context,
            "prefix_visits": state_prefix_visits[state],
            "session_samples": samples,
            "failure_sessions": failures,
            "failure_risk_score": round(failures / samples, 6) if samples else None,
            "risk_sample_sufficient": samples >= _MIN_RISK_SAMPLES,
            "terminal_visits": state_terminal_visits[state],
            "outgoing_count": outgoing[state],
        })

    transitions = [
        {
            "source_state": source,
            "activity": activity,
            "target_state": target,
            "count": count,
            "probability": round(count / outgoing[source], 6),
        }
        for (source, activity, target), count in sorted(transition_counts.items())
    ]
    alphabet = sorted({activity for sequence in sequences for activity in sequence})
    return {
        "schema_version": 1,
        "shadow_mode": True,
        "early_stop_enabled": False,
        "parameters": {
            "context_window": context_window,
            "state_merge": "last_activity_plus_bounded_trailing_context",
        },
        "samples": {
            "sessions": len(rows),
            "nonempty_sessions": sum(bool(sequence) for sequence in sequences),
            "activities": sum(len(sequence) for sequence in sequences),
            "transitions": total_edges,
            "failed_or_unresolved_sessions": sum(
                _failed_outcome(snapshot, sequence)
                for snapshot, sequence in zip(rows, sequences)
            ),
        },
        "alphabet": alphabet,
        "states": states,
        "transitions": transitions,
        "cycle_rate": round(total_cycle_edges / total_edges, 6) if total_edges else 0.0,
        "calibration_boundary": {
            "risk_method": "empirical_failed_or_unresolved_signal_rate_by_reached_state",
            "minimum_state_samples": _MIN_RISK_SAMPLES,
            "scope": "saved local investigation sessions supplied to this model",
            "calibrated": False,
            "causal_interpretation": False,
        },
    }


def analyze_trajectory(
    snapshot: Mapping[str, Any],
    training_snapshots: Sequence[Mapping[str, Any]],
    *,
    context_window: int = 2,
    top_k: int = 3,
) -> dict[str, Any]:
    """Score one saved session against a model that excludes that target session."""
    model = build_trajectory_model(training_snapshots, context_window=context_window)
    sequence = extract_activity_sequence(snapshot)
    current_state = _state_for(sequence, context_window)
    state_row = next(
        (row for row in model["states"] if row["state_id"] == current_state),
        None,
    )
    state_observed = bool(state_row and state_row["prefix_visits"] > 0)
    outgoing = [
        row for row in model["transitions"] if row["source_state"] == current_state
    ]
    outgoing.sort(key=lambda row: (-row["count"], row["activity"], row["target_state"]))
    cycle_edges, total_edges = _cycle_counts(sequence, context_window)

    if sequence:
        source_state = _state_for(sequence[:-1], context_window)
        latest_activity = sequence[-1]
        matching = [
            row
            for row in model["transitions"]
            if row["source_state"] == source_state
            and row["activity"] == latest_activity
            and row["target_state"] == current_state
        ]
        observed_count = int(matching[0]["count"]) if matching else 0
        unseen_transition: dict[str, Any] = {
            "unseen": observed_count == 0,
            "source_state": source_state,
            "activity": latest_activity,
            "target_state": current_state,
            "historical_count": observed_count,
        }
    else:
        unseen_transition = {
            "unseen": False,
            "source_state": None,
            "activity": None,
            "target_state": "start",
            "historical_count": 0,
            "reason": "session_has_no_valid_activities",
        }

    state_samples = int(state_row["session_samples"]) if state_row else 0
    risk_score = state_row["failure_risk_score"] if state_row else None
    risk_reasons: list[str] = []
    if not state_observed:
        risk_reasons.append("current_state_unseen_in_training")
    else:
        risk_reasons.append(
            "historical_failed_or_unresolved_signals="
            f"{state_row['failure_sessions']}/{state_samples}"
        )
    if unseen_transition["unseen"]:
        risk_reasons.append("latest_transition_unseen_in_training")
    if cycle_edges:
        risk_reasons.append(f"revisited_state_edges={cycle_edges}/{total_edges}")
    if state_samples < _MIN_RISK_SAMPLES:
        risk_reasons.append(
            f"state_samples_below_interpretation_floor={state_samples}/{_MIN_RISK_SAMPLES}"
        )
    failure_signal = (
        "unseen_state"
        if not state_observed
        else "elevated"
        if (risk_score or 0.0) >= 0.5
        else "observed"
    )
    return {
        "session_id": _session_id(snapshot) or None,
        "case_id": _case_id(snapshot) or None,
        "activity_count": len(sequence),
        "activities": list(sequence),
        "current_state": {
            "state_id": current_state,
            "last_activity": sequence[-1] if sequence else None,
            "context": list(_context(sequence, context_window)),
            "historically_observed": state_observed,
        },
        "top_next_actions": outgoing[: max(1, min(int(top_k), 10))],
        "unseen_transition": unseen_transition,
        "cycle_rate": round(cycle_edges / total_edges, 6) if total_edges else 0.0,
        "cycle_edges": cycle_edges,
        "failure_risk": {
            "failure_signal": failure_signal,
            "risk_score": risk_score,
            "reasons": risk_reasons,
            "calibrated": False,
            "state_session_samples": state_samples,
            "failed_or_unresolved_samples": (
                int(state_row["failure_sessions"]) if state_row else 0
            ),
            "sample_sufficient": state_samples >= _MIN_RISK_SAMPLES,
        },
        "calibration_boundary": {
            **model["calibration_boundary"],
            "training_sessions": model["samples"]["sessions"],
            "target_session_excluded": True,
            "prediction_effect": "shadow_only",
        },
        "shadow_mode": True,
        "early_stop_enabled": False,
    }


def trajectory_fsm_report(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    session_id: str | None = None,
    case_id: str | None = None,
    target_snapshot: Mapping[str, Any] | None = None,
    context_window: int = 2,
    top_k: int = 3,
    max_events_per_session: int = _TRACE_EVENT_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Return the overall model or a leave-one-out analysis for one saved session."""
    if max_events_per_session <= 0:
        raise ValueError("max_events_per_session must be positive")
    bounded_rows = [
        _bounded_snapshot(snapshot, max_events_per_session)
        for snapshot in snapshots
        if isinstance(snapshot, Mapping)
    ]
    rows = [snapshot for snapshot, _truncated in bounded_rows]
    truncated_sessions = sum(truncated for _snapshot, truncated in bounded_rows)
    limits = {
        "max_events_per_session": max_events_per_session,
        "truncated_sessions": truncated_sessions,
    }
    if session_id or case_id:
        candidates = [
            snapshot
            for snapshot in rows
            if (not session_id or _session_id(snapshot) == session_id)
            and (not case_id or _case_id(snapshot) == case_id)
        ]
        if target_snapshot is not None:
            bounded_target, target_truncated = _bounded_snapshot(
                target_snapshot, max_events_per_session
            )
            target_id = _session_id(bounded_target)
            if (not session_id or target_id == session_id) and (
                not case_id or _case_id(bounded_target) == case_id
            ):
                candidates = [
                    bounded_target,
                    *[
                        snapshot
                        for snapshot in candidates
                        if _session_id(snapshot) != target_id
                    ],
                ]
                if target_id not in {_session_id(snapshot) for snapshot in rows}:
                    rows.append(bounded_target)
                    truncated_sessions += int(target_truncated)
                    limits["truncated_sessions"] = truncated_sessions
        if not candidates:
            raise KeyError("unknown investigation session or case")
        candidates.sort(
            key=lambda snapshot: (
                str(snapshot.get("opened_at") or snapshot.get("openedAt") or ""),
                _session_id(snapshot),
            ),
            reverse=True,
        )
        target = candidates[0]
        target_id = _session_id(target)
        training = [snapshot for snapshot in rows if _session_id(snapshot) != target_id]
        return {
            "mode": "session",
            "selector": {
                "requested_session_id": session_id,
                "requested_case_id": case_id,
                "matching_sessions": len(candidates),
                "selected_session_id": target_id,
            },
            "analysis": analyze_trajectory(
                target,
                training,
                context_window=context_window,
                top_k=top_k,
            ),
            "model": build_trajectory_model(training, context_window=context_window),
            "limits": limits,
        }
    return {
        "mode": "summary",
        "model": build_trajectory_model(rows, context_window=context_window),
        "limits": limits,
    }


__all__ = [
    "analyze_trajectory",
    "build_trajectory_model",
    "extract_activity_sequence",
    "trajectory_fsm_report",
]
