from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from core.remediate.safety import (
    ActionLevel,
    ActionPolicy,
    DomainBusyError,
    DomainLock,
    EmergencyStop,
    RemediationBudget,
)


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_action_policy_levels_and_prerequisites_are_serializable():
    policy = ActionPolicy.for_level(ActionLevel.L2)
    denied = policy.evaluate(impacted_assets=4, automatic=True)
    assert denied.allowed is False
    assert denied.reasons == (
        "impact_limit_exceeded",
        "condition_missing:verified_signal",
        "condition_missing:precheck_passed",
        "condition_missing:health_probe_ready",
        "checkpoint_required",
        "rollback_required",
        "confirmed_commit_required",
    )

    allowed = policy.evaluate(
        impacted_assets=2,
        satisfied_conditions={"verified_signal", "precheck_passed", "health_probe_ready"},
        checkpoint_available=True,
        rollback_available=True,
        confirmed_commit_available=True,
    )
    assert allowed.allowed is True
    assert ActionPolicy.from_dict(policy.to_dict()) == policy
    assert json.loads(json.dumps(allowed.to_dict()))["allowed"] is True


def test_l3_requires_manual_path_and_human_approval():
    policy = ActionPolicy.for_level("L3")
    auto = policy.evaluate(
        impacted_assets=1,
        checkpoint_available=True,
        rollback_available=True,
        confirmed_commit_available=True,
        human_approved=True,
    )
    manual = policy.evaluate(
        impacted_assets=1,
        checkpoint_available=True,
        rollback_available=True,
        confirmed_commit_available=True,
        human_approved=True,
        automatic=False,
    )
    assert auto.reasons == ("automatic_execution_disabled",)
    assert manual.allowed is True


def test_action_policy_rejects_ambiguous_boolean_and_condition_inputs():
    with pytest.raises(ValueError, match="auto_execute"):
        ActionPolicy("L1", "yes", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection"):
        ActionPolicy("L1", True, 1, "verified")  # type: ignore[arg-type]


def _budget(clock: Clock, **overrides) -> RemediationBudget:
    config = {
        "max_per_incident": 2,
        "max_per_asset": 2,
        "max_per_failure_domain": 2,
        "window_seconds": 100,
        "max_concurrency": 2,
        "cooldown_seconds": 10,
        "backoff_base_seconds": 4,
        "backoff_max_seconds": 30,
        "clock": clock,
        "jitter": lambda base: base * 0.25,
    }
    config.update(overrides)
    return RemediationBudget(**config)


def test_budget_execution_ids_are_deterministic_and_duplicate_safe():
    clock = Clock()
    budget = _budget(clock)
    first = budget.acquire("inc-1", "asset-1", "rack-a", "restart", idempotency_key="step-1")
    duplicate = budget.acquire(
        "inc-1", "asset-1", "rack-a", "restart", idempotency_key="step-1"
    )
    assert first.allowed is True
    assert duplicate.allowed is False
    assert duplicate.idempotent is True
    assert duplicate.execution_id == first.execution_id
    assert len(budget.to_dict()["records"]) == 1


def test_budget_enforces_concurrency_and_all_three_rolling_limits():
    clock = Clock()
    concurrency = _budget(clock, max_concurrency=1)
    assert concurrency.acquire("i1", "a1", "d1", "x").allowed
    assert concurrency.acquire("i2", "a2", "d2", "x").reasons == (
        "concurrency_limit_reached",
    )

    budget = _budget(clock, max_concurrency=10, cooldown_seconds=0)
    one = budget.acquire("incident", "asset", "domain", "x", idempotency_key="1")
    budget.complete(one.execution_id, success=True)
    two = budget.acquire("incident", "asset", "domain", "x", idempotency_key="2")
    budget.complete(two.execution_id, success=True)
    denied = budget.acquire("incident", "asset", "domain", "x", idempotency_key="3")
    assert denied.reasons == (
        "incident_budget_exhausted",
        "asset_budget_exhausted",
        "failure_domain_budget_exhausted",
    )
    clock.advance(101)
    assert budget.acquire("incident", "asset", "domain", "x", idempotency_key="3").allowed


def test_budget_cooldown_exponential_backoff_jitter_and_round_trip():
    clock = Clock()
    budget = _budget(clock, cooldown_seconds=1)
    first = budget.acquire("i", "a", "d", "x", idempotency_key="1")
    budget.complete(first.execution_id, success=False)
    blocked = budget.acquire("i", "a", "d", "x", idempotency_key="2")
    assert blocked.reasons == ("cooldown_active",)
    assert blocked.retry_after_seconds == 5.0
    clock.advance(5)
    second = budget.acquire("i", "a", "d", "x", idempotency_key="2")
    assert second.allowed
    budget.complete(second.execution_id, success=False)
    assert budget.backoff_delay(2) == 10.0

    restored = RemediationBudget.from_dict(
        json.loads(json.dumps(budget.to_dict())),
        clock=clock,
        jitter=lambda base: base * 0.25,
    )
    assert restored.to_dict() == budget.to_dict()


def test_budget_completion_is_idempotent_but_outcome_cannot_change():
    clock = Clock()
    budget = _budget(clock)
    admitted = budget.acquire("i", "a", "d", "x")
    first = budget.complete(admitted.execution_id, success=True)
    second = budget.complete(admitted.execution_id, success=True)
    assert first is second
    with pytest.raises(RuntimeError, match="outcome cannot be changed"):
        budget.complete(admitted.execution_id, success=False)


def test_domain_lock_context_releases_and_domains_are_independent():
    lock = DomainLock(clock=lambda: 100.0)
    with lock.acquire("rack-a", "exec-1") as lease:
        assert lock.is_locked("rack-a")
        assert lock.try_acquire("rack-a", "exec-2") is None
        other = lock.acquire("rack-b", "exec-3")
        other.release()
        assert lease.to_dict()["released"] is False
    assert not lock.is_locked("rack-a")
    assert lock.to_dict() == {"leases": []}


def test_domain_lock_is_thread_safe_and_supports_expiring_leases():
    clock = Clock()
    lock = DomainLock(clock=clock)
    barrier = threading.Barrier(8)
    winners: list[str] = []

    def contend(number: int) -> None:
        barrier.wait()
        lease = lock.try_acquire("rack-a", f"worker-{number}")
        if lease is not None:
            winners.append(lease.owner)

    threads = [threading.Thread(target=contend, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    with pytest.raises(DomainBusyError):
        lock.acquire("rack-a", "late")

    expiring = DomainLock(clock=clock)
    old = expiring.acquire("rack-b", "old", lease_seconds=5)
    clock.advance(5)
    new = expiring.acquire("rack-b", "new")
    assert new.owner == "new"
    old.release()
    assert expiring.is_locked("rack-b")


def test_emergency_stop_missing_and_corrupt_state_fail_closed(tmp_path):
    fixed = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
    path = tmp_path / "emergency-stop.json"
    stop = EmergencyStop(path, clock=lambda: fixed)
    missing = stop.status()
    assert missing.paused and missing.fail_closed
    assert missing.reason == "state_read_error:FileNotFoundError"

    path.write_text("not-json", encoding="utf-8")
    corrupt = stop.status()
    assert corrupt.paused and corrupt.fail_closed
    assert corrupt.reason == "state_read_error:JSONDecodeError"


def test_emergency_stop_pause_resume_persists_auditable_state(tmp_path):
    path = tmp_path / "emergency-stop.json"
    clock = lambda: datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
    stop = EmergencyStop(path, clock=clock)
    paused = stop.pause("unexpected blast radius", "oncall@example")
    assert paused.paused is True
    assert paused.reason == "unexpected blast radius"
    assert EmergencyStop(path, clock=clock).status() == paused

    resumed = stop.resume("incident-commander", "risk reviewed")
    assert resumed.paused is False
    assert resumed.actor == "incident-commander"
    assert resumed.timestamp == "2026-08-23T18:00:00Z"
    assert json.loads(path.read_text(encoding="utf-8")) == resumed.to_dict()


def test_emergency_stop_rejects_unattributed_transitions(tmp_path):
    stop = EmergencyStop(tmp_path / "stop.json")
    with pytest.raises(ValueError, match="reason"):
        stop.pause("", "operator")
    with pytest.raises(ValueError, match="actor"):
        stop.resume(" ")
