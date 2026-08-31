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
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.remediate import BakeIn, FollowUp, HealthProbe
from core.remediate.recovery_graph import ActionNode, RecoveryGraph
from core.remediate.safety import (
    ActionLevel,
    ActionPolicy,
    DomainLock,
    EmergencyStop,
    RemediationBudget,
)
from domains.network_rca.remediation import (
    Command,
    CommandLog,
    UnsafeTarget,
    bounce_interface,
    gateway_probe,
    interface_probe,
    read_interface,
    read_unit,
    restart_unit,
    unit_probe,
)

RUNS_PATH: Path | None = None
_SAFETY_LOCK = threading.RLock()
_DOMAIN_LOCKS = DomainLock()
_BUDGET: RemediationBudget | None = None
_BUDGET_LOAD_ERROR: str | None = None


def _runs_path() -> Path:
    """Resolve the history sink when it is used, with an injectable override."""
    if RUNS_PATH is not None:
        return RUNS_PATH
    configured = os.getenv("AUTOPOIESIS_REMEDIATION_LOG")
    if configured:
        return Path(configured)
    test_root = os.getenv("AUTOPOIESIS_TEST_TMP")
    if test_root:
        return Path(test_root) / "remediation-runs.jsonl"
    # PYTEST_CURRENT_TEST appears only while a test body is running. Collection
    # imports happen earlier, so using it as the sole guard can freeze a module
    # constant to the production path for the entire session.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(tempfile.gettempdir()) / "autopoiesis-remediation-test.jsonl"
    return Path("/data/autopoiesis-production/remediation-runs.jsonl")


def _control_path() -> Path:
    configured = os.getenv("AUTOPOIESIS_REMEDIATION_STOP")
    if configured:
        return Path(configured)
    test_root = os.getenv("AUTOPOIESIS_TEST_TMP")
    if test_root:
        return Path(test_root) / "remediation-emergency-stop.json"
    return Path("/data/autopoiesis-production/remediation-emergency-stop.json")


def _budget_path() -> Path:
    configured = os.getenv("AUTOPOIESIS_REMEDIATION_BUDGET")
    if configured:
        return Path(configured)
    test_root = os.getenv("AUTOPOIESIS_TEST_TMP")
    if test_root:
        return Path(test_root) / "remediation-budget.json"
    return Path("/data/autopoiesis-production/remediation-budget.json")


def emergency_stop() -> EmergencyStop:
    """Return the durable process-independent write pause switch."""
    return EmergencyStop(_control_path())


def _new_budget() -> RemediationBudget:
    return RemediationBudget(
        max_per_incident=int(os.getenv("AUTOPOIESIS_REMEDIATION_MAX_PER_INCIDENT", "2")),
        max_per_asset=int(os.getenv("AUTOPOIESIS_REMEDIATION_MAX_PER_ASSET", "2")),
        max_per_failure_domain=int(
            os.getenv("AUTOPOIESIS_REMEDIATION_MAX_PER_DOMAIN", "2")
        ),
        window_seconds=float(os.getenv("AUTOPOIESIS_REMEDIATION_BUDGET_WINDOW", "3600")),
        max_concurrency=int(os.getenv("AUTOPOIESIS_REMEDIATION_MAX_CONCURRENCY", "1")),
        cooldown_seconds=float(os.getenv("AUTOPOIESIS_REMEDIATION_COOLDOWN", "600")),
        backoff_base_seconds=float(os.getenv("AUTOPOIESIS_REMEDIATION_BACKOFF_BASE", "60")),
        backoff_max_seconds=float(os.getenv("AUTOPOIESIS_REMEDIATION_BACKOFF_MAX", "3600")),
    )


def _load_budget() -> RemediationBudget:
    global _BUDGET, _BUDGET_LOAD_ERROR
    with _SAFETY_LOCK:
        if _BUDGET is not None:
            return _BUDGET
        path = _budget_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                _BUDGET = RemediationBudget.from_dict(raw)
                # A prior gateway can die while a dual-window observation is
                # open. Reconcile its durable reservation as failed, which
                # applies cooldown/backoff and frees global concurrency. Leave
                # it in-flight forever and every later safe action is blocked;
                # drop it silently and a crash becomes a budget bypass.
                interrupted = _BUDGET.in_flight_execution_ids()
                for execution_id in interrupted:
                    _BUDGET.complete(execution_id, success=False)
                if interrupted:
                    _write_budget_snapshot(_BUDGET)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                _BUDGET_LOAD_ERROR = f"{type(error).__name__}: {error}"
                _BUDGET = _new_budget()
        else:
            _BUDGET = _new_budget()
        return _BUDGET


def _write_budget_snapshot(budget: RemediationBudget) -> None:
    path = _budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(budget.to_dict(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _persist_budget() -> None:
    with _SAFETY_LOCK:
        _write_budget_snapshot(_load_budget())


def safety_status() -> dict[str, Any]:
    stop = emergency_stop().status()
    budget = _load_budget()
    return {
        "emergency_stop": stop.to_dict(),
        "budget": budget.to_dict(),
        "budget_load_error": _BUDGET_LOAD_ERROR,
        "domain_locks": _DOMAIN_LOCKS.to_dict(),
        "limits": {
            "max_actions_per_incident": 2,
            "fast_window_seconds": DEFAULT_BAKE_IN.window_seconds,
            "stability_window_seconds": DEFAULT_BAKE_IN.stability_window_seconds,
        },
    }

# Windows are short here because both actions settle in seconds, not minutes.
# A firewall change would want minutes; these do not, and a window longer than
# the failure mode it is watching for only delays the verdict.
DEFAULT_BAKE_IN = BakeIn(
    window_seconds=60.0,
    stability_window_seconds=180.0,
    interval_seconds=15.0,
    grace_seconds=5.0,
    consecutive_bad=2,
    success_consecutive=3,
)


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
        policy: ActionPolicy | None = None,
        mechanism: str = "host_recovery",
    ) -> None:
        self.name = name
        self.summary = summary
        self.family = family
        self.preflight = preflight
        self.commit = commit
        self.probes = probes
        self.revert = revert
        self.policy = policy or ActionPolicy.for_level(ActionLevel.L1)
        self.mechanism = mechanism

    def recovery_node(self) -> ActionNode:
        return ActionNode(
            action_id=self.name,
            mechanism=self.mechanism,
            impact=self.policy.max_impacted_assets,
            required_metrics=("live_probe_count",),
            # The graph records the required recovery contract even for a
            # monotonic action whose baseline is already the failed state. If
            # no inverse exists, a bad observation escalates at this gate.
            rollback_id=(f"revert:{self.name}" if self.revert else "verified-baseline-return"),
        )


_HOST_L1_POLICY = ActionPolicy(
    level=ActionLevel.L1,
    auto_execute=True,
    max_impacted_assets=1,
    automatic_conditions=("verified_signal", "precheck_passed"),
    requires_checkpoint=True,
    # These actions start from a worst-state target. Their durable live
    # baseline is the recovery point; collateral regression still stops and
    # escalates because neither action has a constructive inverse.
    requires_rollback=False,
)


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
        policy=_HOST_L1_POLICY,
        mechanism="interface_reinitialize",
    ),
    "restart_unit": Action(
        name="restart_unit",
        summary="Restart a systemd unit that is already failed, within its budget",
        family="fam-perception-selfheal",
        preflight=_unit_preflight,
        commit=lambda command, target: restart_unit(command, target),
        probes=lambda command, target: [unit_probe(command, target), gateway_probe(command)],
        revert=None,
        policy=_HOST_L1_POLICY,
        mechanism="service_restart",
    ),
}


def describe_actions() -> list[dict[str, Any]]:
    return [
        {
            "name": action.name,
            "summary": action.summary,
            "family": action.family,
            "mechanism": action.mechanism,
            "policy": action.policy.to_dict(),
        }
        for action in ACTIONS.values()
    ]


def _resolve(action_name: str) -> Action:
    action = ACTIONS.get(action_name)
    if action is None:
        raise UnsafeTarget(f"unknown action {action_name!r}; the action set is a closed list")
    return action


def preflight(
    action_name: str,
    target: str,
    command: Command | None = None,
    on_command: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Report whether this action would run, and why, without running it.

    ``on_command`` receives each command as it runs. A caller that supplies its
    own ``command`` is already carrying its own transport, and whatever journal
    it wants lives there — so the sink only applies to the one built here.
    """
    action = _resolve(action_name)
    stop = emergency_stop().status()
    if stop.paused:
        return {
            "action": action_name,
            "target": target,
            "eligible": False,
            "refused": True,
            "reason": f"global remediation pause is active: {stop.reason}",
            "emergency_stop": stop.to_dict(),
            "policy": action.policy.to_dict(),
        }
    if _BUDGET_LOAD_ERROR:
        return {
            "action": action_name,
            "target": target,
            "eligible": False,
            "refused": True,
            "reason": f"budget ledger is unreadable: {_BUDGET_LOAD_ERROR}",
            "policy": action.policy.to_dict(),
        }
    log = CommandLog(on_entry=on_command) if command is None else None
    command = command or Command.local(log)
    try:
        outcome = action.preflight(command, target)
    except UnsafeTarget as refusal:
        from .blast_radius import estimate

        # A refusal is exactly when the operator most wants to know what the
        # action would have touched, so the radius is attached here too.
        return {
            "action": action_name,
            "target": target,
            "eligible": False,
            "refused": True,
            "commands": log.entries if log else [],
            "reason": str(refusal),
            "blast_radius": estimate(action_name, target),
        }
    from .blast_radius import estimate

    policy_decision = action.policy.evaluate(
        impacted_assets=1,
        satisfied_conditions=("verified_signal", "precheck_passed")
        if outcome.get("eligible")
        else (),
        checkpoint_available=bool(outcome.get("reading")),
        rollback_available=action.revert is not None,
        confirmed_commit_available=False,
        human_approved=False,
        automatic=bool(outcome.get("eligible")),
    )
    eligible = bool(outcome.get("eligible")) and policy_decision.allowed
    reason = outcome.get("reason", "")
    if outcome.get("eligible") and not policy_decision.allowed:
        reason = ", ".join(policy_decision.reasons)
    return {
        "action": action_name,
        "target": target,
        "family": action.family,
        "refused": False,
        "reverts": action.revert is not None,
        "policy": action.policy.to_dict(),
        "policy_decision": policy_decision.to_dict(),
        "commands": log.entries if log else [],
        # Measured now, on this box, rather than described in the abstract:
        # the operator needs a number they can check, not a reassurance.
        "blast_radius": estimate(action_name, target),
        **outcome,
        "eligible": eligible,
        "reason": reason,
    }


def execute(
    action_name: str,
    target: str,
    command: Command | None = None,
    bake_in: BakeIn | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    on_command: Callable[[dict[str, Any]], None] | None = None,
    *,
    incident_id: str | None = None,
    failure_domain: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Run the action under a watch window and return the full verdict.

    ``sleep`` is injectable so a test can drive a full window without spending
    it: a suite that really waits out every bake-in stops being run.
    """
    action = _resolve(action_name)
    log = CommandLog(on_entry=on_command)
    command = command or Command.local(log)

    # preflight runs through the same journaling command, so its reads land in
    # the one transcript rather than a second, separate list.
    check = preflight(action_name, target, command)
    if not check.get("eligible"):
        return {"ran": False, "verdict": None, **check}

    incident_id = incident_id or f"adhoc:{uuid.uuid4().hex}"
    failure_domain = failure_domain or action.family
    idempotency_key = idempotency_key or uuid.uuid4().hex
    budget = _load_budget()
    execution_id = budget.execution_id(
        incident_id,
        target,
        failure_domain,
        action_name,
        idempotency_key,
    )
    lease = _DOMAIN_LOCKS.try_acquire(failure_domain, execution_id)
    if lease is None:
        return {
            "ran": False,
            "verdict": None,
            **check,
            "eligible": False,
            "refused": True,
            "reason": f"failure domain is busy: {failure_domain}",
            "execution_id": execution_id,
        }
    budget_decision = budget.acquire(
        incident_id,
        target,
        failure_domain,
        action_name,
        idempotency_key=idempotency_key,
        execution_id=execution_id,
    )
    if not budget_decision.allowed:
        lease.release()
        return {
            "ran": False,
            "verdict": None,
            **check,
            "eligible": False,
            "refused": True,
            "reason": ", ".join(budget_decision.reasons),
            "budget_decision": budget_decision.to_dict(),
            "execution_id": execution_id,
        }
    _persist_budget()

    graph = RecoveryGraph([action.recovery_node()], max_actions_per_incident=2)
    graph.open_incident(incident_id, action_name, at=datetime.now(timezone.utc))
    graph.precheck(
        incident_id,
        at=datetime.now(timezone.utc),
        metrics={"live_probe_count": len(action.probes(command, target))},
        management_reachable=True,
    )

    events: list[dict[str, Any]] = []

    def record(kind: str, payload: dict[str, Any]) -> None:
        events.append({"kind": kind, "payload": payload})
        if emit is not None:
            emit(kind, payload)

    follow_up = FollowUp(bake_in=bake_in or DEFAULT_BAKE_IN, emit=record)
    if sleep is not None:
        follow_up.sleep = sleep
    budget_completed = False
    try:
        try:
            verdict = follow_up.run(
                action=f"{action_name}:{target}",
                probes=action.probes(command, target),
                commit=lambda: action.commit(command, target),
                revert=(lambda: action.revert(command, target)) if action.revert else None,
            )
        except UnsafeTarget as refusal:
            budget.complete(execution_id, success=False)
            budget_completed = True
            _persist_budget()
            return {
                "ran": False,
                "action": action_name,
                "target": target,
                "refused": True,
                "reason": str(refusal),
                "execution_id": execution_id,
            }

        now = datetime.now(timezone.utc)
        if verdict.committed:
            graph.commit(
                incident_id,
                at=now,
                management_reachable=True,
                readback_passed=True,
            )
            graph.begin_fast_observation(
                incident_id,
                at=now,
                metrics={"live_probe_count": max(1, verdict.fast_samples)},
                management_reachable=True,
            )
            if verdict.outcome == "passed":
                graph.begin_stability_observation(
                    incident_id,
                    at=now,
                    metrics={"live_probe_count": max(1, verdict.fast_samples)},
                    management_reachable=True,
                    fast_window_passed=True,
                )
                graph.pass_stability(
                    incident_id,
                    at=now,
                    metrics={
                        "live_probe_count": max(
                            1, verdict.stability_samples or verdict.fast_samples
                        )
                    },
                    management_reachable=True,
                    stability_window_passed=True,
                )
            elif verdict.outcome == "reverted":
                graph.revert(
                    incident_id,
                    at=now,
                    rollback_succeeded=True,
                    rollback_metrics={"live_probe_count": 1},
                    management_reachable=True,
                )
            else:
                graph.revert(
                    incident_id,
                    at=now,
                    rollback_succeeded=False,
                    rollback_metrics=None,
                    management_reachable=True,
                )
        else:
            graph.escalate(incident_id, at=now, reason="commit_not_landed")

        budget.complete(execution_id, success=verdict.outcome == "passed")
        budget_completed = True
        _persist_budget()
    except Exception:
        if not budget_completed:
            budget.complete(execution_id, success=False)
            _persist_budget()
        raise
    finally:
        lease.release()

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
        "commands": log.entries,
        "execution_id": execution_id,
        "incident_id": incident_id,
        "case_id": incident_id if incident_id.startswith("case-") else None,
        "failure_domain": failure_domain,
        "budget_decision": budget_decision.to_dict(),
        "recovery_run": graph.get_run(incident_id).to_dict(),
    }
    _append_run(record_payload)
    return {"ran": True, **record_payload}


def _append_run(payload: dict[str, Any]) -> None:
    """Persist one run. A failure to log must not mask the run's own result."""
    path = _runs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def history(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent runs first."""
    path = _runs_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(rows))[:limit]
