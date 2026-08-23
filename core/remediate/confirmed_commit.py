"""Checkpoint-gated confirmed commits for managed network devices.

The adapter follows the RFC 6241 candidate workflow: lock, edit-config with
``rollback-on-error``, validate, confirmed commit with a device-side timeout,
readback, explicit confirmation or cancellation, and unlock.  Cisco and Junos
adapters expose their native confirmed-timeout operation through the same typed
transport.  This module accepts structured configuration documents only; it has
no command-string or shell execution path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping, Protocol

from core.remediate.device_checkpoint import (
    CheckpointUnavailable,
    DeviceCheckpoint,
    DeviceCheckpointTransport,
    ManagementReadback,
    capture_device_checkpoint,
)


CommitStyle = Literal["rfc6241", "cisco", "junos"]
CommitOutcome = Literal["confirmed", "refused", "auto_reverted", "revert_unverified"]


def _required(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ConfirmedCommitCapabilities:
    style: CommitStyle
    candidate: bool
    validate: bool
    rollback_on_error: bool
    confirmed_timeout: bool

    @property
    def safe(self) -> bool:
        return (
            self.candidate
            and self.validate
            and self.rollback_on_error
            and self.confirmed_timeout
        )


@dataclass(frozen=True, slots=True)
class ManagementExpectation:
    """Concrete post-change signals required before confirmation."""

    config_version: str | None = None
    configuration_sha256: str | None = None
    required_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.config_version is not None:
            object.__setattr__(
                self, "config_version", _required(self.config_version, "config_version")
            )
        if self.configuration_sha256 is not None:
            digest = self.configuration_sha256.strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("configuration_sha256 must be a lowercase SHA-256 digest")
            object.__setattr__(self, "configuration_sha256", digest)
        checks = tuple(sorted({_required(value, "required check") for value in self.required_checks}))
        object.__setattr__(self, "required_checks", checks)
        if self.config_version is None and self.configuration_sha256 is None and not checks:
            raise ValueError("management expectation requires a version, digest, or named check")

    def matches(self, readback: ManagementReadback) -> bool:
        if not readback.healthy:
            return False
        if self.config_version is not None and readback.config_version != self.config_version:
            return False
        if (
            self.configuration_sha256 is not None
            and readback.configuration_sha256 != self.configuration_sha256
        ):
            return False
        checks = readback.check_map()
        return all(checks.get(name) is True for name in self.required_checks)


@dataclass(frozen=True, slots=True)
class CandidateEdit:
    change_id: str
    configuration: Mapping[str, object]
    expectation: ManagementExpectation

    def __post_init__(self) -> None:
        object.__setattr__(self, "change_id", _required(self.change_id, "change_id"))
        if not isinstance(self.configuration, Mapping) or not self.configuration:
            raise ValueError("candidate configuration must not be empty")
        # This is the only accepted edit representation.  JSON validation keeps
        # adapters on structured API payloads and rejects executable objects.
        encoded = json.dumps(
            self.configuration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(self, "configuration", json.loads(encoded))


class ConfirmedCommitTransport(DeviceCheckpointTransport, Protocol):
    """Typed NETCONF/vendor adapter boundary for one management session."""

    def confirmed_commit_capabilities(self, device_id: str) -> ConfirmedCommitCapabilities: ...

    def lock_candidate(self, device_id: str) -> None: ...

    def edit_candidate(
        self,
        device_id: str,
        edit: CandidateEdit,
        *,
        error_option: Literal["rollback-on-error"],
    ) -> None: ...

    def validate_candidate(self, device_id: str) -> None: ...

    def discard_candidate(self, device_id: str) -> None: ...

    def commit_confirmed(self, device_id: str, *, timeout_seconds: int) -> None: ...

    def confirm_commit(self, device_id: str) -> None: ...

    def cancel_commit(self, device_id: str) -> None: ...

    def wait_for_confirmed_rollback(self, device_id: str, *, timeout_seconds: int) -> None: ...

    def unlock_candidate(self, device_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CommitStep:
    name: str
    status: Literal["passed", "failed", "skipped"]
    at: datetime
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedCommitReceipt:
    receipt_id: str
    device_id: str
    change_id: str
    outcome: CommitOutcome
    started_at: datetime
    completed_at: datetime
    confirmed_timeout_seconds: int
    checkpoint: DeviceCheckpoint | None
    post_change_readback: ManagementReadback | None
    rollback_readback: ManagementReadback | None
    cancel_requested: bool
    rollback_verified: bool | None
    reason: str
    steps: tuple[CommitStep, ...]


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class ConfirmedCommitExecutor:
    def __init__(
        self,
        transport: ConfirmedCommitTransport,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _at(self) -> datetime:
        return _utc(self._now())

    def apply(
        self,
        device_id: str,
        edit: CandidateEdit,
        *,
        confirmed_timeout_seconds: int = 120,
    ) -> ConfirmedCommitReceipt:
        """Apply one change and confirm it only after a healthy expected readback."""
        device_id = _required(device_id, "device_id")
        if confirmed_timeout_seconds <= 0:
            raise ValueError("confirmed_timeout_seconds must be positive")
        started_at = self._at()
        steps: list[CommitStep] = []
        checkpoint: DeviceCheckpoint | None = None
        post_readback: ManagementReadback | None = None
        locked = False
        confirmed_started = False

        def step(name: str, status: Literal["passed", "failed", "skipped"], detail: str | None = None) -> None:
            steps.append(CommitStep(name=name, status=status, at=self._at(), detail=detail))

        def unlock() -> None:
            nonlocal locked
            if not locked:
                return
            try:
                self._transport.unlock_candidate(device_id)
                step("unlock_candidate", "passed")
            except Exception as exc:  # noqa: BLE001 - receipt keeps cleanup failure
                step("unlock_candidate", "failed", _error(exc))
            locked = False

        try:
            checkpoint = capture_device_checkpoint(self._transport, device_id)
            step("checkpoint", "passed", checkpoint.checkpoint_id)
        except (CheckpointUnavailable, ValueError) as exc:
            step("checkpoint", "failed", _error(exc))
            return self._receipt(
                device_id=device_id,
                edit=edit,
                outcome="refused",
                started_at=started_at,
                timeout=confirmed_timeout_seconds,
                checkpoint=None,
                post_readback=None,
                rollback_readback=None,
                cancel_requested=False,
                rollback_verified=None,
                reason=f"checkpoint gate failed: {_error(exc)}",
                steps=steps,
            )

        try:
            capabilities = self._transport.confirmed_commit_capabilities(device_id)
            if not capabilities.safe:
                step("capability_check", "failed", f"unsafe capabilities: {capabilities}")
                return self._receipt(
                    device_id=device_id,
                    edit=edit,
                    outcome="refused",
                    started_at=started_at,
                    timeout=confirmed_timeout_seconds,
                    checkpoint=checkpoint,
                    post_readback=None,
                    rollback_readback=None,
                    cancel_requested=False,
                    rollback_verified=None,
                    reason="device lacks candidate, validate, rollback-on-error, or confirmed-timeout support",
                    steps=steps,
                )
            step("capability_check", "passed", capabilities.style)

            self._transport.lock_candidate(device_id)
            locked = True
            step("lock_candidate", "passed")
            self._transport.edit_candidate(
                device_id, edit, error_option="rollback-on-error"
            )
            step("edit_candidate", "passed", "error-option=rollback-on-error")
            self._transport.validate_candidate(device_id)
            step("validate_candidate", "passed")
            self._transport.commit_confirmed(
                device_id, timeout_seconds=confirmed_timeout_seconds
            )
            confirmed_started = True
            step("commit_confirmed", "passed", f"timeout={confirmed_timeout_seconds}s")

            post_readback = self._transport.read_management_plane(device_id)
            if post_readback.degraded_from(checkpoint.management_readback):
                raise _PostCommitUnsafe("management-plane readback degraded")
            if not edit.expectation.matches(post_readback):
                raise _PostCommitUnsafe("post-change readback did not match the declared expectation")
            step("post_change_readback", "passed")

            try:
                self._transport.confirm_commit(device_id)
            except TimeoutError as exc:
                raise _PostCommitUnsafe(f"confirmation timed out: {exc}") from exc
            step("confirm_commit", "passed")
            unlock()
            return self._receipt(
                device_id=device_id,
                edit=edit,
                outcome="confirmed",
                started_at=started_at,
                timeout=confirmed_timeout_seconds,
                checkpoint=checkpoint,
                post_readback=post_readback,
                rollback_readback=None,
                cancel_requested=False,
                rollback_verified=None,
                reason="confirmed commit passed management-plane readback",
                steps=steps,
            )
        except _PostCommitUnsafe as exc:
            step("safety_gate", "failed", str(exc))
            receipt = self._rollback(
                device_id=device_id,
                edit=edit,
                checkpoint=checkpoint,
                post_readback=post_readback,
                started_at=started_at,
                timeout=confirmed_timeout_seconds,
                reason=str(exc),
                steps=steps,
            )
            unlock()
            return self._replace_steps(receipt, steps)
        except Exception as exc:  # noqa: BLE001 - phase determines safe compensation
            if confirmed_started:
                step("confirmed_commit_flow", "failed", _error(exc))
                receipt = self._rollback(
                    device_id=device_id,
                    edit=edit,
                    checkpoint=checkpoint,
                    post_readback=post_readback,
                    started_at=started_at,
                    timeout=confirmed_timeout_seconds,
                    reason=_error(exc),
                    steps=steps,
                )
                unlock()
                return self._replace_steps(receipt, steps)
            step("candidate_flow", "failed", _error(exc))
            if locked:
                try:
                    self._transport.discard_candidate(device_id)
                    step("discard_candidate", "passed")
                except Exception as discard_exc:  # noqa: BLE001
                    step("discard_candidate", "failed", _error(discard_exc))
            unlock()
            return self._receipt(
                device_id=device_id,
                edit=edit,
                outcome="refused",
                started_at=started_at,
                timeout=confirmed_timeout_seconds,
                checkpoint=checkpoint,
                post_readback=post_readback,
                rollback_readback=None,
                cancel_requested=False,
                rollback_verified=None,
                reason=f"candidate transaction refused: {_error(exc)}",
                steps=steps,
            )

    def _rollback(
        self,
        *,
        device_id: str,
        edit: CandidateEdit,
        checkpoint: DeviceCheckpoint,
        post_readback: ManagementReadback | None,
        started_at: datetime,
        timeout: int,
        reason: str,
        steps: list[CommitStep],
    ) -> ConfirmedCommitReceipt:
        cancel_requested = True
        try:
            self._transport.cancel_commit(device_id)
            steps.append(CommitStep("cancel_commit", "passed", self._at()))
        except Exception as exc:  # noqa: BLE001 - device timeout remains the fallback
            steps.append(CommitStep("cancel_commit", "failed", self._at(), _error(exc)))

        try:
            self._transport.wait_for_confirmed_rollback(
                device_id, timeout_seconds=timeout
            )
            steps.append(CommitStep("wait_for_confirmed_rollback", "passed", self._at()))
        except Exception as exc:  # noqa: BLE001
            steps.append(
                CommitStep("wait_for_confirmed_rollback", "failed", self._at(), _error(exc))
            )

        rollback_readback: ManagementReadback | None = None
        try:
            rollback_readback = self._transport.read_management_plane(device_id)
            restored = (
                rollback_readback.healthy
                and rollback_readback.config_version == checkpoint.config_version
                and rollback_readback.configuration_sha256 == checkpoint.configuration_sha256
            )
            steps.append(
                CommitStep(
                    "rollback_readback",
                    "passed" if restored else "failed",
                    self._at(),
                    "checkpoint version and digest restored" if restored else "checkpoint restoration not proven",
                )
            )
        except Exception as exc:  # noqa: BLE001
            restored = False
            steps.append(CommitStep("rollback_readback", "failed", self._at(), _error(exc)))

        return self._receipt(
            device_id=device_id,
            edit=edit,
            outcome="auto_reverted" if restored else "revert_unverified",
            started_at=started_at,
            timeout=timeout,
            checkpoint=checkpoint,
            post_readback=post_readback,
            rollback_readback=rollback_readback,
            cancel_requested=cancel_requested,
            rollback_verified=restored,
            reason=(
                f"{reason}; checkpoint restoration verified"
                if restored
                else f"{reason}; checkpoint restoration remains unverified"
            ),
            steps=steps,
        )

    def _receipt(
        self,
        *,
        device_id: str,
        edit: CandidateEdit,
        outcome: CommitOutcome,
        started_at: datetime,
        timeout: int,
        checkpoint: DeviceCheckpoint | None,
        post_readback: ManagementReadback | None,
        rollback_readback: ManagementReadback | None,
        cancel_requested: bool,
        rollback_verified: bool | None,
        reason: str,
        steps: list[CommitStep],
    ) -> ConfirmedCommitReceipt:
        return ConfirmedCommitReceipt(
            receipt_id=f"confirmed-commit:{device_id}:{edit.change_id}:{started_at.isoformat()}",
            device_id=device_id,
            change_id=edit.change_id,
            outcome=outcome,
            started_at=started_at,
            completed_at=self._at(),
            confirmed_timeout_seconds=timeout,
            checkpoint=checkpoint,
            post_change_readback=post_readback,
            rollback_readback=rollback_readback,
            cancel_requested=cancel_requested,
            rollback_verified=rollback_verified,
            reason=reason,
            steps=tuple(steps),
        )

    def _replace_steps(
        self,
        receipt: ConfirmedCommitReceipt, steps: list[CommitStep]
    ) -> ConfirmedCommitReceipt:
        """Return the rollback receipt after the later candidate unlock is recorded."""
        return ConfirmedCommitReceipt(
            receipt_id=receipt.receipt_id,
            device_id=receipt.device_id,
            change_id=receipt.change_id,
            outcome=receipt.outcome,
            started_at=receipt.started_at,
            completed_at=self._at(),
            confirmed_timeout_seconds=receipt.confirmed_timeout_seconds,
            checkpoint=receipt.checkpoint,
            post_change_readback=receipt.post_change_readback,
            rollback_readback=receipt.rollback_readback,
            cancel_requested=receipt.cancel_requested,
            rollback_verified=receipt.rollback_verified,
            reason=receipt.reason,
            steps=tuple(steps),
        )


class _PostCommitUnsafe(RuntimeError):
    pass
