from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.remediate.confirmed_commit import (
    CandidateEdit,
    ConfirmedCommitCapabilities,
    ConfirmedCommitExecutor,
    ManagementExpectation,
)
from core.remediate.device_checkpoint import (
    CheckpointCapture,
    ManagementReadback,
    RestoreAction,
)


NOW = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
OLD_DIGEST = "a" * 64
NEW_DIGEST = "b" * 64


def readback(
    *,
    version: str = "v1",
    digest: str = OLD_DIGEST,
    reachable: bool = True,
    session_healthy: bool = True,
    routing: bool = True,
    seconds: int = 1,
) -> ManagementReadback:
    return ManagementReadback(
        device_id="edge-01",
        collected_at=NOW + timedelta(seconds=seconds),
        reachable=reachable,
        session_healthy=session_healthy,
        config_version=version,
        configuration_sha256=digest,
        checks=(("management_route", routing),),
    )


def capture() -> CheckpointCapture:
    return CheckpointCapture(
        device_id="edge-01",
        configuration_summary={"interfaces": 48, "vlans": 12},
        configuration_sha256=OLD_DIGEST,
        backup_locator="s3://network-checkpoints/edge-01/v1.xml",
        config_version="v1",
        restore_action=RestoreAction(
            method="netconf_copy_config",
            backup_locator="s3://network-checkpoints/edge-01/v1.xml",
            expected_config_version="v1",
        ),
        captured_at=NOW,
    )


def edit() -> CandidateEdit:
    return CandidateEdit(
        change_id="change-42",
        configuration={
            "interfaces": {
                "interface": [
                    {"name": "GigabitEthernet0/1", "description": "uplink"}
                ]
            }
        },
        expectation=ManagementExpectation(
            config_version="v2",
            configuration_sha256=NEW_DIGEST,
            required_checks=("management_route",),
        ),
    )


class FakeTransport:
    def __init__(
        self,
        *,
        style: str = "rfc6241",
        readbacks: list[ManagementReadback] | None = None,
    ) -> None:
        self.style = style
        self.readbacks = list(readbacks or [readback()])
        self.calls: list[tuple] = []
        self.capture_error: Exception | None = None
        self.validate_error: Exception | None = None
        self.confirm_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.rollback_read_error: Exception | None = None
        self.safe_capabilities = True

    def capture_checkpoint(self, device_id: str) -> CheckpointCapture:
        self.calls.append(("capture_checkpoint", device_id))
        if self.capture_error:
            raise self.capture_error
        return capture()

    def read_management_plane(self, device_id: str) -> ManagementReadback:
        self.calls.append(("read_management_plane", device_id))
        if self.rollback_read_error and len(self.readbacks) == 1:
            raise self.rollback_read_error
        return self.readbacks.pop(0)

    def confirmed_commit_capabilities(self, device_id: str) -> ConfirmedCommitCapabilities:
        self.calls.append(("capabilities", device_id))
        return ConfirmedCommitCapabilities(
            style=self.style,
            candidate=self.safe_capabilities,
            validate=self.safe_capabilities,
            rollback_on_error=self.safe_capabilities,
            confirmed_timeout=self.safe_capabilities,
        )

    def lock_candidate(self, device_id: str) -> None:
        self.calls.append(("lock_candidate", device_id))

    def edit_candidate(self, device_id: str, candidate: CandidateEdit, *, error_option: str) -> None:
        self.calls.append(("edit_candidate", device_id, candidate.change_id, error_option))

    def validate_candidate(self, device_id: str) -> None:
        self.calls.append(("validate_candidate", device_id))
        if self.validate_error:
            raise self.validate_error

    def discard_candidate(self, device_id: str) -> None:
        self.calls.append(("discard_candidate", device_id))

    def commit_confirmed(self, device_id: str, *, timeout_seconds: int) -> None:
        self.calls.append(("commit_confirmed", device_id, timeout_seconds))

    def confirm_commit(self, device_id: str) -> None:
        self.calls.append(("confirm_commit", device_id))
        if self.confirm_error:
            raise self.confirm_error

    def cancel_commit(self, device_id: str) -> None:
        self.calls.append(("cancel_commit", device_id))
        if self.cancel_error:
            raise self.cancel_error

    def wait_for_confirmed_rollback(self, device_id: str, *, timeout_seconds: int) -> None:
        self.calls.append(("wait_for_confirmed_rollback", device_id, timeout_seconds))
        if self.wait_error:
            raise self.wait_error

    def unlock_candidate(self, device_id: str) -> None:
        self.calls.append(("unlock_candidate", device_id))


def clock():
    instant = NOW

    def now() -> datetime:
        nonlocal instant
        instant += timedelta(milliseconds=1)
        return instant

    return now


@pytest.mark.parametrize("style", ["rfc6241", "cisco", "junos"])
def test_success_uses_candidate_confirmed_commit_and_structured_readback(style: str) -> None:
    transport = FakeTransport(
        style=style,
        readbacks=[
            readback(),
            readback(version="v2", digest=NEW_DIGEST, seconds=2),
        ],
    )
    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply(
        "edge-01", edit(), confirmed_timeout_seconds=180
    )

    assert receipt.outcome == "confirmed"
    assert receipt.checkpoint.config_version == "v1"
    assert receipt.post_change_readback.config_version == "v2"
    assert receipt.cancel_requested is False
    assert transport.calls == [
        ("capture_checkpoint", "edge-01"),
        ("read_management_plane", "edge-01"),
        ("capabilities", "edge-01"),
        ("lock_candidate", "edge-01"),
        ("edit_candidate", "edge-01", "change-42", "rollback-on-error"),
        ("validate_candidate", "edge-01"),
        ("commit_confirmed", "edge-01", 180),
        ("read_management_plane", "edge-01"),
        ("confirm_commit", "edge-01"),
        ("unlock_candidate", "edge-01"),
    ]
    assert [step.name for step in receipt.steps][-2:] == ["confirm_commit", "unlock_candidate"]


def test_checkpoint_failure_refuses_before_capability_lock_or_edit() -> None:
    transport = FakeTransport()
    transport.capture_error = OSError("backup store unavailable")

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply("edge-01", edit())

    assert receipt.outcome == "refused"
    assert receipt.checkpoint is None
    assert "checkpoint gate failed" in receipt.reason
    assert transport.calls == [("capture_checkpoint", "edge-01")]


def test_unsafe_capabilities_refuse_without_touching_candidate() -> None:
    transport = FakeTransport()
    transport.safe_capabilities = False

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply("edge-01", edit())

    assert receipt.outcome == "refused"
    assert transport.calls[-1] == ("capabilities", "edge-01")
    assert not any(call[0] == "lock_candidate" for call in transport.calls)


def test_validate_failure_discards_candidate_and_unlocks_without_running_commit() -> None:
    transport = FakeTransport()
    transport.validate_error = ValueError("schema validation failed")

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply("edge-01", edit())

    assert receipt.outcome == "refused"
    names = [call[0] for call in transport.calls]
    assert names[-2:] == ["discard_candidate", "unlock_candidate"]
    assert "commit_confirmed" not in names
    assert "cancel_commit" not in names


def test_management_link_loss_cancels_and_verifies_automatic_restore() -> None:
    transport = FakeTransport(
        readbacks=[
            readback(),
            readback(
                version="v2",
                digest=NEW_DIGEST,
                reachable=False,
                session_healthy=False,
                seconds=2,
            ),
            readback(version="v1", digest=OLD_DIGEST, seconds=190),
        ]
    )

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply(
        "edge-01", edit(), confirmed_timeout_seconds=180
    )

    assert receipt.outcome == "auto_reverted"
    assert receipt.cancel_requested is True
    assert receipt.rollback_verified is True
    assert receipt.rollback_readback.config_version == "v1"
    names = [call[0] for call in transport.calls]
    assert names[-4:] == [
        "cancel_commit",
        "wait_for_confirmed_rollback",
        "read_management_plane",
        "unlock_candidate",
    ]


def test_confirmation_timeout_uses_device_timeout_and_verifies_restore() -> None:
    transport = FakeTransport(
        readbacks=[
            readback(),
            readback(version="v2", digest=NEW_DIGEST, seconds=2),
            readback(version="v1", digest=OLD_DIGEST, seconds=130),
        ]
    )
    transport.confirm_error = TimeoutError("RPC deadline elapsed")

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply("edge-01", edit())

    assert receipt.outcome == "auto_reverted"
    assert "confirmation timed out" in receipt.reason
    assert ("wait_for_confirmed_rollback", "edge-01", 120) in transport.calls


def test_readback_regression_and_failed_cancel_leave_revert_unverified() -> None:
    transport = FakeTransport(
        readbacks=[
            readback(),
            readback(version="v2", digest=NEW_DIGEST, routing=False, seconds=2),
            readback(
                version="v2",
                digest=NEW_DIGEST,
                reachable=False,
                session_healthy=False,
                routing=False,
                seconds=130,
            ),
        ]
    )
    transport.cancel_error = ConnectionError("management session lost")
    transport.wait_error = TimeoutError("no reconnect before deadline")

    receipt = ConfirmedCommitExecutor(transport, now=clock()).apply("edge-01", edit())

    assert receipt.outcome == "revert_unverified"
    assert receipt.cancel_requested is True
    assert receipt.rollback_verified is False
    assert "remains unverified" in receipt.reason
    failed = {step.name for step in receipt.steps if step.status == "failed"}
    assert {"safety_gate", "cancel_commit", "wait_for_confirmed_rollback", "rollback_readback"} <= failed


def test_configuration_edit_has_no_shell_or_command_string_interface() -> None:
    candidate = edit()

    assert isinstance(candidate.configuration, dict)
    assert not hasattr(candidate, "shell")
    assert not hasattr(candidate, "command")
    with pytest.raises((TypeError, ValueError)):
        CandidateEdit(  # type: ignore[arg-type]
            change_id="bad",
            configuration="configure terminal; shutdown",
            expectation=ManagementExpectation(config_version="v2"),
        )
