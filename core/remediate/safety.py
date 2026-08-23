"""Safety controls for bounded, repeatable remediation execution.

The module intentionally contains policy and coordination primitives only.  It
does not execute commands, inspect production systems, or depend on the gateway.
All public state objects expose JSON-compatible ``to_dict`` representations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping


class ActionLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    automatic: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "automatic": self.automatic,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ActionPolicy:
    """Declarative prerequisites for one class of remediation action."""

    level: ActionLevel | str
    auto_execute: bool
    max_impacted_assets: int
    automatic_conditions: tuple[str, ...] = ()
    requires_checkpoint: bool = False
    requires_rollback: bool = False
    requires_confirmed_commit: bool = False
    requires_human_approval: bool = False

    def __post_init__(self) -> None:
        try:
            level = self.level if isinstance(self.level, ActionLevel) else ActionLevel(self.level)
        except ValueError as exc:
            raise ValueError(f"unsupported action level: {self.level!r}") from exc
        if (
            isinstance(self.max_impacted_assets, bool)
            or not isinstance(self.max_impacted_assets, int)
            or self.max_impacted_assets < 0
        ):
            raise ValueError("max_impacted_assets must be a non-negative integer")
        for name in (
            "auto_execute",
            "requires_checkpoint",
            "requires_rollback",
            "requires_confirmed_commit",
            "requires_human_approval",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if isinstance(self.automatic_conditions, str):
            raise ValueError("automatic_conditions must be a collection of names")
        conditions = tuple(str(item).strip() for item in self.automatic_conditions)
        if any(not item for item in conditions) or len(set(conditions)) != len(conditions):
            raise ValueError("automatic_conditions must be unique, non-empty names")
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "automatic_conditions", conditions)

    @classmethod
    def for_level(cls, level: ActionLevel | str) -> "ActionPolicy":
        normalized = level if isinstance(level, ActionLevel) else ActionLevel(level)
        defaults = {
            ActionLevel.L0: cls(ActionLevel.L0, True, 0),
            ActionLevel.L1: cls(
                ActionLevel.L1,
                True,
                1,
                ("verified_signal", "precheck_passed"),
                requires_checkpoint=True,
                requires_rollback=True,
            ),
            ActionLevel.L2: cls(
                ActionLevel.L2,
                True,
                3,
                ("verified_signal", "precheck_passed", "health_probe_ready"),
                requires_checkpoint=True,
                requires_rollback=True,
                requires_confirmed_commit=True,
            ),
            ActionLevel.L3: cls(
                ActionLevel.L3,
                False,
                1,
                requires_checkpoint=True,
                requires_rollback=True,
                requires_confirmed_commit=True,
                requires_human_approval=True,
            ),
        }
        return defaults[normalized]

    def evaluate(
        self,
        *,
        impacted_assets: int,
        satisfied_conditions: Iterable[str] = (),
        checkpoint_available: bool = False,
        rollback_available: bool = False,
        confirmed_commit_available: bool = False,
        human_approved: bool = False,
        automatic: bool = True,
    ) -> PolicyDecision:
        if (
            isinstance(impacted_assets, bool)
            or not isinstance(impacted_assets, int)
            or impacted_assets < 0
        ):
            raise ValueError("impacted_assets must be a non-negative integer")
        satisfied = frozenset(str(item) for item in satisfied_conditions)
        reasons: list[str] = []
        if impacted_assets > self.max_impacted_assets:
            reasons.append("impact_limit_exceeded")
        if automatic and not self.auto_execute:
            reasons.append("automatic_execution_disabled")
        if automatic:
            missing = [item for item in self.automatic_conditions if item not in satisfied]
            reasons.extend(f"condition_missing:{item}" for item in missing)
        if self.requires_checkpoint and not checkpoint_available:
            reasons.append("checkpoint_required")
        if self.requires_rollback and not rollback_available:
            reasons.append("rollback_required")
        if self.requires_confirmed_commit and not confirmed_commit_available:
            reasons.append("confirmed_commit_required")
        if self.requires_human_approval and not human_approved:
            reasons.append("human_approval_required")
        return PolicyDecision(not reasons, automatic, tuple(reasons))

    def allows_automatic_execution(self, **kwargs: object) -> bool:
        return self.evaluate(automatic=True, **kwargs).allowed

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "auto_execute": self.auto_execute,
            "max_impacted_assets": self.max_impacted_assets,
            "automatic_conditions": list(self.automatic_conditions),
            "requires_checkpoint": self.requires_checkpoint,
            "requires_rollback": self.requires_rollback,
            "requires_confirmed_commit": self.requires_confirmed_commit,
            "requires_human_approval": self.requires_human_approval,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ActionPolicy":
        def boolean(name: str, default: bool = False) -> bool:
            value = data.get(name, default)
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            return value

        return cls(
            level=str(data["level"]),
            auto_execute=boolean("auto_execute"),
            max_impacted_assets=int(data["max_impacted_assets"]),
            automatic_conditions=tuple(str(item) for item in data.get("automatic_conditions", ())),
            requires_checkpoint=boolean("requires_checkpoint"),
            requires_rollback=boolean("requires_rollback"),
            requires_confirmed_commit=boolean("requires_confirmed_commit"),
            requires_human_approval=boolean("requires_human_approval"),
        )


@dataclass
class BudgetRecord:
    execution_id: str
    incident_id: str
    asset_id: str
    failure_domain: str
    action: str
    started_at: float
    completed_at: float | None = None
    success: bool | None = None

    @property
    def in_flight(self) -> bool:
        return self.completed_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "incident_id": self.incident_id,
            "asset_id": self.asset_id,
            "failure_domain": self.failure_domain,
            "action": self.action,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "BudgetRecord":
        completed = data.get("completed_at")
        success = data.get("success")
        return cls(
            execution_id=str(data["execution_id"]),
            incident_id=str(data["incident_id"]),
            asset_id=str(data["asset_id"]),
            failure_domain=str(data["failure_domain"]),
            action=str(data["action"]),
            started_at=float(data["started_at"]),
            completed_at=None if completed is None else float(completed),
            success=None if success is None else bool(success),
        )


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    execution_id: str
    reasons: tuple[str, ...] = ()
    retry_after_seconds: float = 0.0
    idempotent: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "execution_id": self.execution_id,
            "reasons": list(self.reasons),
            "retry_after_seconds": self.retry_after_seconds,
            "idempotent": self.idempotent,
        }


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class RemediationBudget:
    """Thread-safe rolling budgets plus cooldown and failure backoff."""

    def __init__(
        self,
        *,
        max_per_incident: int = 3,
        max_per_asset: int = 2,
        max_per_failure_domain: int = 3,
        window_seconds: float = 3600.0,
        max_concurrency: int = 1,
        cooldown_seconds: float = 60.0,
        backoff_base_seconds: float = 5.0,
        backoff_max_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.max_per_incident = _positive_int("max_per_incident", max_per_incident)
        self.max_per_asset = _positive_int("max_per_asset", max_per_asset)
        self.max_per_failure_domain = _positive_int(
            "max_per_failure_domain", max_per_failure_domain
        )
        self.max_concurrency = _positive_int("max_concurrency", max_concurrency)
        for name, value in (
            ("window_seconds", window_seconds),
            ("cooldown_seconds", cooldown_seconds),
            ("backoff_base_seconds", backoff_base_seconds),
            ("backoff_max_seconds", backoff_max_seconds),
        ):
            if isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if window_seconds == 0:
            raise ValueError("window_seconds must be greater than zero")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be at least backoff_base_seconds")
        self.window_seconds = float(window_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_max_seconds = float(backoff_max_seconds)
        self._clock = clock
        self._jitter = jitter or (lambda _delay: 0.0)
        self._records: dict[str, BudgetRecord] = {}
        self._failure_streaks: dict[tuple[str, str], int] = {}
        self._not_before: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def execution_id(
        incident_id: str,
        asset_id: str,
        failure_domain: str,
        action: str,
        idempotency_key: str = "",
    ) -> str:
        parts = [incident_id, asset_id, failure_domain, action, idempotency_key]
        if any(not isinstance(part, str) or not part.strip() for part in parts[:4]):
            raise ValueError("incident, asset, failure domain, and action must be non-empty")
        canonical = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
        return "rem-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def backoff_delay(self, consecutive_failures: int) -> float:
        if consecutive_failures <= 0:
            return 0.0
        base = min(
            self.backoff_max_seconds,
            self.backoff_base_seconds * (2 ** (consecutive_failures - 1)),
        )
        return max(0.0, base + float(self._jitter(base)))

    def _now(self, supplied: float | None) -> float:
        instant = float(self._clock() if supplied is None else supplied)
        if not math.isfinite(instant):
            raise ValueError("time must be finite")
        return instant

    def _recent(self, now: float) -> list[BudgetRecord]:
        threshold = now - self.window_seconds
        return [r for r in self._records.values() if r.in_flight or r.started_at >= threshold]

    def acquire(
        self,
        incident_id: str,
        asset_id: str,
        failure_domain: str,
        action: str,
        *,
        idempotency_key: str = "",
        execution_id: str | None = None,
        now: float | None = None,
    ) -> BudgetDecision:
        for name, value in (
            ("incident_id", incident_id),
            ("asset_id", asset_id),
            ("failure_domain", failure_domain),
            ("action", action),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        execution_id = execution_id or self.execution_id(
            incident_id, asset_id, failure_domain, action, idempotency_key
        )
        if not execution_id.strip():
            raise ValueError("execution_id must be non-empty")
        instant = self._now(now)
        with self._lock:
            prior = self._records.get(execution_id)
            if prior is not None:
                return BudgetDecision(
                    False,
                    execution_id,
                    ("duplicate_execution",),
                    idempotent=True,
                )
            recent = self._recent(instant)
            reasons: list[str] = []
            if sum(record.in_flight for record in recent) >= self.max_concurrency:
                reasons.append("concurrency_limit_reached")
            if sum(record.incident_id == incident_id for record in recent) >= self.max_per_incident:
                reasons.append("incident_budget_exhausted")
            if sum(record.asset_id == asset_id for record in recent) >= self.max_per_asset:
                reasons.append("asset_budget_exhausted")
            if (
                sum(record.failure_domain == failure_domain for record in recent)
                >= self.max_per_failure_domain
            ):
                reasons.append("failure_domain_budget_exhausted")
            target = (asset_id, failure_domain)
            retry_after = max(0.0, self._not_before.get(target, 0.0) - instant)
            if retry_after > 0:
                reasons.append("cooldown_active")
            if reasons:
                return BudgetDecision(False, execution_id, tuple(reasons), retry_after)
            self._records[execution_id] = BudgetRecord(
                execution_id,
                incident_id,
                asset_id,
                failure_domain,
                action,
                instant,
            )
            return BudgetDecision(True, execution_id)

    try_acquire = acquire

    def in_flight_execution_ids(self) -> tuple[str, ...]:
        """Stable IDs that need startup reconciliation after a process exit."""
        with self._lock:
            return tuple(
                sorted(
                    record.execution_id
                    for record in self._records.values()
                    if record.in_flight
                )
            )

    def complete(
        self,
        execution_id: str,
        *,
        success: bool,
        now: float | None = None,
    ) -> BudgetRecord:
        if not isinstance(success, bool):
            raise ValueError("success must be boolean")
        instant = self._now(now)
        with self._lock:
            try:
                record = self._records[execution_id]
            except KeyError as exc:
                raise KeyError(f"unknown execution_id: {execution_id}") from exc
            if not record.in_flight:
                if record.success is success:
                    return record
                raise RuntimeError("completed execution outcome cannot be changed")
            if instant < record.started_at:
                raise ValueError("completion time cannot precede start time")
            record.completed_at = instant
            record.success = bool(success)
            target = (record.asset_id, record.failure_domain)
            if success:
                self._failure_streaks.pop(target, None)
                delay = self.cooldown_seconds
            else:
                streak = self._failure_streaks.get(target, 0) + 1
                self._failure_streaks[target] = streak
                delay = max(self.cooldown_seconds, self.backoff_delay(streak))
            self._not_before[target] = instant + delay
            return record

    release = complete

    def to_dict(self) -> dict[str, object]:
        with self._lock:
            return {
                "config": {
                    "max_per_incident": self.max_per_incident,
                    "max_per_asset": self.max_per_asset,
                    "max_per_failure_domain": self.max_per_failure_domain,
                    "window_seconds": self.window_seconds,
                    "max_concurrency": self.max_concurrency,
                    "cooldown_seconds": self.cooldown_seconds,
                    "backoff_base_seconds": self.backoff_base_seconds,
                    "backoff_max_seconds": self.backoff_max_seconds,
                },
                "records": [
                    self._records[key].to_dict() for key in sorted(self._records)
                ],
                "failure_streaks": [
                    {"asset_id": key[0], "failure_domain": key[1], "count": value}
                    for key, value in sorted(self._failure_streaks.items())
                ],
                "not_before": [
                    {"asset_id": key[0], "failure_domain": key[1], "at": value}
                    for key, value in sorted(self._not_before.items())
                ],
            }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float], float] | None = None,
    ) -> "RemediationBudget":
        config = dict(data["config"])  # type: ignore[arg-type]
        budget = cls(**config, clock=clock, jitter=jitter)
        for raw in data.get("records", ()):  # type: ignore[assignment]
            record = BudgetRecord.from_dict(raw)
            budget._records[record.execution_id] = record
        for raw in data.get("failure_streaks", ()):  # type: ignore[assignment]
            key = (str(raw["asset_id"]), str(raw["failure_domain"]))
            budget._failure_streaks[key] = int(raw["count"])
        for raw in data.get("not_before", ()):  # type: ignore[assignment]
            key = (str(raw["asset_id"]), str(raw["failure_domain"]))
            budget._not_before[key] = float(raw["at"])
        return budget


class DomainBusyError(RuntimeError):
    pass


@dataclass
class _HeldDomain:
    token: str
    owner: str
    acquired_at: float
    expires_at: float | None


@dataclass
class DomainLease:
    domain: str
    owner: str
    token: str
    acquired_at: float
    expires_at: float | None
    _manager: "DomainLock" = field(repr=False, compare=False)
    _released: bool = field(default=False, init=False, repr=False, compare=False)

    def release(self) -> None:
        if not self._released:
            self._manager._release(self.domain, self.token)
            self._released = True

    def __enter__(self) -> "DomainLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "owner": self.owner,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "released": self._released,
        }


class DomainLock:
    """One in-flight lease per failure domain, safe across threads."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._held: dict[str, _HeldDomain] = {}
        self._sequence = 0

    def _purge_expired(self, domain: str, now: float) -> None:
        held = self._held.get(domain)
        if held is not None and held.expires_at is not None and held.expires_at <= now:
            del self._held[domain]
            self._condition.notify_all()

    def try_acquire(
        self,
        domain: str,
        owner: str,
        *,
        lease_seconds: float | None = None,
    ) -> DomainLease | None:
        if not domain.strip() or not owner.strip():
            raise ValueError("domain and owner must be non-empty")
        if lease_seconds is not None and lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._condition:
            return self._try_acquire_locked(domain, owner, lease_seconds)

    def _try_acquire_locked(
        self,
        domain: str,
        owner: str,
        lease_seconds: float | None,
    ) -> DomainLease | None:
        now = float(self._clock())
        self._purge_expired(domain, now)
        if domain in self._held:
            return None
        self._sequence += 1
        token = f"domain-lease-{self._sequence}"
        expires_at = None if lease_seconds is None else now + lease_seconds
        self._held[domain] = _HeldDomain(token, owner, now, expires_at)
        return DomainLease(domain, owner, token, now, expires_at, self)

    def acquire(
        self,
        domain: str,
        owner: str,
        *,
        blocking: bool = False,
        timeout: float | None = None,
        lease_seconds: float | None = None,
    ) -> DomainLease:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        if not domain.strip() or not owner.strip():
            raise ValueError("domain and owner must be non-empty")
        if lease_seconds is not None and lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                lease = self._try_acquire_locked(domain, owner, lease_seconds)
                if lease is not None:
                    return lease
                if not blocking:
                    raise DomainBusyError(f"failure domain is busy: {domain}")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise DomainBusyError(f"timed out waiting for failure domain: {domain}")
                self._condition.wait(remaining)

    lease = acquire

    def _release(self, domain: str, token: str) -> None:
        with self._condition:
            held = self._held.get(domain)
            if held is None:
                return
            if held.token != token:
                # An expired lease may be released after a successor acquired
                # the domain. It must never unlock or disturb that successor.
                return
            del self._held[domain]
            self._condition.notify_all()

    def is_locked(self, domain: str) -> bool:
        with self._condition:
            self._purge_expired(domain, float(self._clock()))
            return domain in self._held

    def to_dict(self) -> dict[str, object]:
        with self._condition:
            now = float(self._clock())
            for domain in tuple(self._held):
                self._purge_expired(domain, now)
            return {
                "leases": [
                    {
                        "domain": domain,
                        "owner": held.owner,
                        "token": held.token,
                        "acquired_at": held.acquired_at,
                        "expires_at": held.expires_at,
                    }
                    for domain, held in sorted(self._held.items())
                ]
            }


@dataclass(frozen=True)
class EmergencyStopState:
    paused: bool
    reason: str
    actor: str
    timestamp: str
    fail_closed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "reason": self.reason,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "fail_closed": self.fail_closed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "EmergencyStopState":
        if not isinstance(data.get("paused"), bool):
            raise ValueError("paused must be boolean")
        reason, actor, timestamp = data.get("reason"), data.get("actor"), data.get("timestamp")
        if not all(isinstance(item, str) and item for item in (reason, actor, timestamp)):
            raise ValueError("reason, actor, and timestamp must be non-empty strings")
        parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        fail_closed = data.get("fail_closed", False)
        if not isinstance(fail_closed, bool):
            raise ValueError("fail_closed must be boolean")
        return cls(bool(data["paused"]), str(reason), str(actor), str(timestamp), fail_closed)


class EmergencyStop:
    """Durable global pause switch; missing or unreadable state fails closed."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime | float] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()

    def _timestamp(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            instant = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            instant = instant.astimezone(timezone.utc)
        else:
            instant = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return instant.isoformat().replace("+00:00", "Z")

    def _fail_closed(self, exc: BaseException) -> EmergencyStopState:
        return EmergencyStopState(
            True,
            f"state_read_error:{type(exc).__name__}",
            "safety-control",
            self._timestamp(),
            True,
        )

    def status(self) -> EmergencyStopState:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("state must be a JSON object")
                return EmergencyStopState.from_dict(raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._fail_closed(exc)

    @property
    def paused(self) -> bool:
        return self.status().paused

    def is_paused(self) -> bool:
        return self.paused

    def _write(self, state: EmergencyStopState) -> EmergencyStopState:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return state

    @staticmethod
    def _required(label: str, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{label} must be non-empty")
        return normalized

    def pause(self, reason: str, actor: str) -> EmergencyStopState:
        with self._lock:
            return self._write(
                EmergencyStopState(
                    True,
                    self._required("reason", reason),
                    self._required("actor", actor),
                    self._timestamp(),
                )
            )

    def resume(self, actor: str, reason: str = "manual resume") -> EmergencyStopState:
        with self._lock:
            return self._write(
                EmergencyStopState(
                    False,
                    self._required("reason", reason),
                    self._required("actor", actor),
                    self._timestamp(),
                )
            )


__all__ = [
    "ActionLevel",
    "ActionPolicy",
    "BudgetDecision",
    "BudgetRecord",
    "DomainBusyError",
    "DomainLease",
    "DomainLock",
    "EmergencyStop",
    "EmergencyStopState",
    "PolicyDecision",
    "RemediationBudget",
]
