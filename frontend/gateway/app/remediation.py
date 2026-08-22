"""Gateway surface for the monotonic host actions and their watch windows.

Three endpoints' worth of logic, kept out of main.py:

``preflight``  what would happen, without touching anything. Every precondition
               runs; a refusal here is the normal outcome and is reported with
               its reason rather than as an error.
``execute``    commit, then hold the window open, then pass or revert. Returns
               the whole verdict including every reading taken.
``history``    past runs, so the console can show what the system did while
               nobody was watching.

The action set is a closed allowlist. A request naming anything outside it is
refused before a single command is built — the same posture the read-only
playbook executor already takes, for the same reason: the caller is the
network, and the network does not get to choose the verb.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.remediate import BakeIn, FollowUp, HealthProbe
from domains.network_rca.remediation import (
    Command,
    UnsafeTarget,
    bounce_interface,
    gateway_probe,
    interface_probe,
    read_interface,
    read_unit,
    restart_unit,
    unit_probe,
)

RUNS_PATH = Path(
    os.getenv("AUTOPOIESIS_REMEDIATION_LOG", "/data/autopoiesis-runtime/remediation-runs.jsonl")
)

# Windows are short here because both actions settle in seconds, not minutes.
# A firewall change would want minutes; these do not, and a window longer than
# the failure mode it is watching for only delays the verdict.
DEFAULT_BAKE_IN = BakeIn(window_seconds=90.0, interval_seconds=15.0, grace_seconds=5.0)


class Action:
    """One monotonic action: how to check it, do it, undo it, and watch it."""

    def __init__(
        self,
        name: str,
        summary: str,
        family: str,
        preflight: Callable[[Command, str], dict[str, Any]],
        commit: Callable[[Command, str], bool],
        probes: Callable[[Command, str], list[HealthProbe]],
        revert: Callable[[Command, str], None] | None = None,
    ) -> None:
        self.name = name
        self.summary = summary
        self.family = family
        self.preflight = preflight
        self.commit = commit
        self.probes = probes
        self.revert = revert


def _interface_preflight(command: Command, target: str) -> dict[str, Any]:
    reading = read_interface(command, target)
    eligible = reading.get("found", False) and not reading.get("carrier", True)
    if eligible:
        reason = "interface is down with no carrier; bouncing it cannot lower a baseline of zero"
    elif reading.get("state") == "REFUSED":
        reason = reading.get("note", "not a physical NIC name")
    elif not reading.get("found"):
        reason = f"no interface named {target!r} on this host"
    else:
        reason = f"interface reads {reading.get('state')}; only a NIC already without carrier qualifies"
    return {"eligible": eligible, "reading": reading, "reason": reason}


def _unit_preflight(command: Command, target: str) -> dict[str, Any]:
    reading = read_unit(command, target)
    eligible = reading.get("failed", False) and reading.get("restarts", 0) < 2
    if reading.get("failed") and reading.get("restarts", 0) >= 2:
        reason = f"already restarted {reading['restarts']} times; further restarts need a person"
    elif eligible:
        reason = "unit is failed and within its restart budget"
    else:
        reason = f"unit reads {reading.get('active')}; only a failed unit qualifies"
    return {"eligible": eligible, "reading": reading, "reason": reason}


ACTIONS: dict[str, Action] = {
    "bounce_interface": Action(
        name="bounce_interface",
        summary="Down/up a physical NIC that already reports no carrier",
        family="fam-host-config-drift",
        preflight=_interface_preflight,
        commit=lambda command, target: bounce_interface(command, target),
        probes=lambda command, target: [interface_probe(command, target), gateway_probe(command)],
        # Bouncing a NIC that was already down has no meaningful inverse: the
        # pre-state *is* down. A regression here escalates instead.
        revert=None,
    ),
    "restart_unit": Action(
        name="restart_unit",
        summary="Restart a systemd unit that is already failed, within its budget",
        family="fam-perception-selfheal",
        preflight=_unit_preflight,
        commit=lambda command, target: restart_unit(command, target),
        probes=lambda command, target: [unit_probe(command, target), gateway_probe(command)],
        revert=None,
    ),
}


def describe_actions() -> list[dict[str, str]]:
    return [
        {"name": action.name, "summary": action.summary, "family": action.family}
        for action in ACTIONS.values()
    ]


def _resolve(action_name: str) -> Action:
    action = ACTIONS.get(action_name)
    if action is None:
        raise UnsafeTarget(f"unknown action {action_name!r}; the action set is a closed list")
    return action


def preflight(action_name: str, target: str, command: Command | None = None) -> dict[str, Any]:
    """Report whether this action would run, and why, without running it."""
    action = _resolve(action_name)
    command = command or Command.local()
    try:
        outcome = action.preflight(command, target)
    except UnsafeTarget as refusal:
        return {
            "action": action_name,
            "target": target,
            "eligible": False,
            "refused": True,
            "reason": str(refusal),
        }
    return {
        "action": action_name,
        "target": target,
        "family": action.family,
        "refused": False,
        "reverts": action.revert is not None,
        **outcome,
    }


def execute(
    action_name: str,
    target: str,
    command: Command | None = None,
    bake_in: BakeIn | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run the action under a watch window and return the full verdict.

    ``sleep`` is injectable so a test can drive a full window without spending
    it: a suite that really waits out every bake-in stops being run.
    """
    action = _resolve(action_name)
    command = command or Command.local()

    check = preflight(action_name, target, command)
    if not check.get("eligible"):
        return {"ran": False, "verdict": None, **check}

    events: list[dict[str, Any]] = []

    def record(kind: str, payload: dict[str, Any]) -> None:
        events.append({"kind": kind, "payload": payload})
        if emit is not None:
            emit(kind, payload)

    follow_up = FollowUp(bake_in=bake_in or DEFAULT_BAKE_IN, emit=record)
    if sleep is not None:
        follow_up.sleep = sleep
    try:
        verdict = follow_up.run(
            action=f"{action_name}:{target}",
            probes=action.probes(command, target),
            commit=lambda: action.commit(command, target),
            revert=(lambda: action.revert(command, target)) if action.revert else None,
        )
    except UnsafeTarget as refusal:
        return {
            "ran": False,
            "action": action_name,
            "target": target,
            "refused": True,
            "reason": str(refusal),
        }

    record_payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "action": action_name,
        "target": target,
        "family": action.family,
        "outcome": verdict.outcome,
        "needs_human": verdict.needs_human,
        "detail": verdict.detail,
        "verdict": verdict.model_dump(mode="json"),
        "events": events,
    }
    _append_run(record_payload)
    return {"ran": True, **record_payload}


def _append_run(payload: dict[str, Any]) -> None:
    """Persist one run. A failure to log must not mask the run's own result."""
    try:
        RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RUNS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def history(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent runs first."""
    if not RUNS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with RUNS_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(rows))[:limit]
