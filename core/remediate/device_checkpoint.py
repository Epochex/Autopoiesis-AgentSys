"""Verified recovery checkpoints for network-device configuration changes.

The checkpoint boundary is deliberately transport-agnostic.  A vendor adapter
must export the running configuration to durable storage and then perform an
independent management-plane readback.  A change executor may proceed only
after :func:`capture_device_checkpoint` returns successfully.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, Protocol


def _required(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def _sha256(value: str, field: str) -> str:
    value = value.strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ManagementReadback:
    """An independently collected management-plane observation."""

    device_id: str
    collected_at: datetime
    reachable: bool
    session_healthy: bool
    config_version: str | None
    configuration_sha256: str | None
    checks: tuple[tuple[str, bool], ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _required(self.device_id, "device_id"))
        object.__setattr__(self, "collected_at", _utc(self.collected_at, "collected_at"))
        if self.config_version is not None:
            object.__setattr__(
                self, "config_version", _required(self.config_version, "config_version")
            )
        if self.configuration_sha256 is not None:
            object.__setattr__(
                self,
                "configuration_sha256",
                _sha256(self.configuration_sha256, "configuration_sha256"),
            )
        normalized: dict[str, bool] = {}
        for name, passed in self.checks:
            name = _required(name, "management check name")
            if name in normalized:
                raise ValueError(f"duplicate management check: {name}")
            if not isinstance(passed, bool):
                raise TypeError(f"management check {name!r} must be boolean")
            normalized[name] = passed
        object.__setattr__(self, "checks", tuple(sorted(normalized.items())))

    @property
    def healthy(self) -> bool:
        return self.reachable and self.session_healthy and all(
            passed for _name, passed in self.checks
        )

    def check_map(self) -> dict[str, bool]:
        return dict(self.checks)

    def degraded_from(self, baseline: "ManagementReadback") -> bool:
        """Return true when a previously healthy management signal regressed."""
        if self.device_id != baseline.device_id:
            return True
        if not self.reachable or not self.session_healthy:
            return True
        current = self.check_map()
        return any(passed and current.get(name) is not True for name, passed in baseline.checks)


@dataclass(frozen=True, slots=True)
class RestoreAction:
    """Structured recovery operation understood by the vendor transport."""

    method: Literal["netconf_copy_config", "vendor_checkpoint", "replace_config"]
    backup_locator: str
    expected_config_version: str

    def __post_init__(self) -> None:
        if self.method not in {
            "netconf_copy_config",
            "vendor_checkpoint",
            "replace_config",
        }:
            raise ValueError(f"unsupported restore method: {self.method}")
        object.__setattr__(
            self, "backup_locator", _required(self.backup_locator, "backup_locator")
        )
        object.__setattr__(
            self,
            "expected_config_version",
            _required(self.expected_config_version, "expected_config_version"),
        )


@dataclass(frozen=True, slots=True)
class CheckpointCapture:
    """Material returned after the adapter durably exports running config."""

    device_id: str
    configuration_summary: Mapping[str, object]
    configuration_sha256: str
    backup_locator: str
    config_version: str
    restore_action: RestoreAction
    captured_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _required(self.device_id, "device_id"))
        if not isinstance(self.configuration_summary, Mapping) or not self.configuration_summary:
            raise ValueError("configuration_summary must not be empty")
        # Reject non-JSON values and detach mutable adapter-owned mappings.
        encoded = json.dumps(
            self.configuration_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(self, "configuration_summary", json.loads(encoded))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256(self.configuration_sha256, "configuration_sha256"),
        )
        object.__setattr__(
            self, "backup_locator", _required(self.backup_locator, "backup_locator")
        )
        object.__setattr__(
            self, "config_version", _required(self.config_version, "config_version")
        )
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        if self.restore_action.backup_locator != self.backup_locator:
            raise ValueError("restore action must reference the captured backup")
        if self.restore_action.expected_config_version != self.config_version:
            raise ValueError("restore action must target the captured config version")


@dataclass(frozen=True, slots=True)
class DeviceCheckpoint:
    device_id: str
    configuration_summary: Mapping[str, object]
    configuration_sha256: str
    backup_locator: str
    config_version: str
    restore_action: RestoreAction
    collected_at: datetime
    management_readback: ManagementReadback
    checkpoint_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _required(self.device_id, "device_id"))
        object.__setattr__(self, "collected_at", _utc(self.collected_at, "collected_at"))
        object.__setattr__(
            self, "checkpoint_id", _required(self.checkpoint_id, "checkpoint_id")
        )
        if not isinstance(self.configuration_summary, Mapping) or not self.configuration_summary:
            raise ValueError("configuration_summary must not be empty")
        encoded = json.dumps(
            self.configuration_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(self, "configuration_summary", json.loads(encoded))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256(self.configuration_sha256, "configuration_sha256"),
        )
        object.__setattr__(
            self, "backup_locator", _required(self.backup_locator, "backup_locator")
        )
        object.__setattr__(
            self, "config_version", _required(self.config_version, "config_version")
        )
        if self.management_readback.device_id != self.device_id:
            raise ValueError("checkpoint management readback belongs to another device")
        if self.restore_action.backup_locator != self.backup_locator:
            raise ValueError("restore action must reference the checkpoint backup")
        if self.restore_action.expected_config_version != self.config_version:
            raise ValueError("restore action must target the checkpoint version")


class DeviceCheckpointTransport(Protocol):
    """Vendor adapter boundary; implementations use APIs, never shell text."""

    def capture_checkpoint(self, device_id: str) -> CheckpointCapture: ...

    def read_management_plane(self, device_id: str) -> ManagementReadback: ...


class CheckpointUnavailable(RuntimeError):
    """The system could not prove that the device can be restored."""


def capture_device_checkpoint(
    transport: DeviceCheckpointTransport, device_id: str
) -> DeviceCheckpoint:
    """Capture and verify a recovery point before any candidate edit or lock."""
    device_id = _required(device_id, "device_id")
    try:
        capture = transport.capture_checkpoint(device_id)
        readback = transport.read_management_plane(device_id)
    except Exception as exc:  # noqa: BLE001 - transport errors become a closed safety gate
        raise CheckpointUnavailable(
            f"checkpoint unavailable for {device_id}: {type(exc).__name__}: {exc}"
        ) from exc
    if capture.device_id != device_id or readback.device_id != device_id:
        raise CheckpointUnavailable("checkpoint or readback returned a different device")
    if not readback.healthy:
        raise CheckpointUnavailable("management-plane checkpoint readback is unhealthy")
    if readback.config_version != capture.config_version:
        raise CheckpointUnavailable("checkpoint version does not match management readback")
    if readback.configuration_sha256 != capture.configuration_sha256:
        raise CheckpointUnavailable("checkpoint digest does not match management readback")
    if readback.collected_at < capture.captured_at:
        raise CheckpointUnavailable("management readback predates checkpoint capture")

    identity = json.dumps(
        {
            "device_id": device_id,
            "configuration_sha256": capture.configuration_sha256,
            "backup_locator": capture.backup_locator,
            "config_version": capture.config_version,
            "captured_at": capture.captured_at.isoformat(),
            "readback_at": readback.collected_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    checkpoint_id = "device-checkpoint:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return DeviceCheckpoint(
        device_id=device_id,
        configuration_summary=capture.configuration_summary,
        configuration_sha256=capture.configuration_sha256,
        backup_locator=capture.backup_locator,
        config_version=capture.config_version,
        restore_action=capture.restore_action,
        collected_at=capture.captured_at,
        management_readback=readback,
        checkpoint_id=checkpoint_id,
    )
