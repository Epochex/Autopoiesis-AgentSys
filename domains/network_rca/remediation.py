"""The monotonic host actions, and the probes that decide whether they held.

Everything registered here satisfies one property: **the target is already at
its worst**. The NIC has no carrier, the unit is already failed, the logs are
already rotated and shipped. Acting cannot drop below a baseline that is
already on the floor, which is what makes an unattended fix defensible at all.
That property is not a comment — it is checked by the preconditions below, and
a target that fails the check is refused before the handler runs.

What this module deliberately does not contain: anything touching the firewall,
a shared subnet, a VLAN, or a third-party device that is currently serving. Those
are not monotonic. They are drafted and a person runs them.

One address is excluded everywhere by name rather than by policy: the Tailscale
path is the only route into this network from outside, so no action here may
select `tailscale0`, `tailscaled`, or anything on 100.64.0.0/10. Cutting it
would strand the operator with no way back in.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.remediate import HealthProbe
from core.safety.tailscale import is_tailscale_target

# Physical NIC names we are willing to bounce. A name that does not match is
# refused rather than guessed at — `lo`, bridges, veths and tunnels all read
# "down" in ways that mean different things.
PHYSICAL_NIC = re.compile(r"^(eno|enp|eth|ens)\d[\w.]*$")


class UnsafeTarget(ValueError):
    """Raised when an action is pointed at something it must never touch."""


@dataclass
class CommandLog:
    """Every command an action really ran, and what came back.

    Without this the system could restart a service, watch it for ninety
    seconds and declare it fixed while the operator saw only the verdict — no
    way to check the reasoning, and nothing to review afterwards. Actions get
    trusted by being watched, so the transcript is part of the product.

    Output is trimmed rather than dropped: a `journalctl` dump would bury the
    log, but a truncated head still shows what the action was reading.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    on_entry: Callable[[dict[str, Any]], None] | None = None
    max_output: int = 400
    limit: int = 200

    stage: str = "preflight"

    def emit(self, entry: dict[str, Any]) -> None:
        if self.on_entry is not None:
            try:
                self.on_entry(entry)
            except Exception:
                pass

    def begin(self, argv: list[str]) -> dict[str, Any]:
        verb = tuple(argv[:2])
        purpose = {
            ('systemctl', 'restart'): 'Restart the verified failed service',
            ('systemctl', 'is-active'): 'Read the service active state',
            ('systemctl', 'show'): 'Read the service restart counter',
            ('journalctl', '-u'): 'Inspect recent service failures and dependency evidence',
            ('ip', '-br'): 'Read the interface carrier and administrative state',
            ('ip', 'link'): 'Apply the reviewed interface administrative state',
            ('curl', '-s'): 'Verify management gateway HTTP reachability',
        }.get(verb, 'Execute the recorded command for this action')
        at = datetime.now(timezone.utc).isoformat()
        entry = {'at': at, 'started_at': at, 'command_id': uuid.uuid4().hex,
                 'argv': list(argv), 'phase': 'started', 'stage': self.stage,
                 'purpose': purpose, 'verification': self.stage == 'verification',
                 'rc': None, 'out': '', 'stdout': '', 'stderr': '', 'truncated': False}
        self.emit(entry)
        return entry

    def add(self, argv: list[str], done: subprocess.CompletedProcess,
            *, started: dict[str, Any] | None = None, duration_ms: float | None = None,
            error: str | None = None) -> None:
        stdout = done.stdout or ''
        stderr = done.stderr or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode('utf-8', errors='replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        raw = (stdout + stderr).strip()
        at = datetime.now(timezone.utc).isoformat()
        entry = {**(started or {}), 'at': at, 'finished_at': at,
                 'argv': list(argv), 'phase': 'error' if error else 'completed',
                 'rc': done.returncode, 'out': raw[:self.max_output],
                 'stdout': stdout[:self.max_output], 'stderr': stderr[:self.max_output],
                 'truncated': len(raw) > self.max_output, 'error': error,
                 'duration_ms': duration_ms}
        if len(self.entries) < self.limit:
            self.entries.append(entry)
        self.emit(entry)


@dataclass
class Command:
    """A shell command plus how to read its result.

    Injected so tests drive the whole path without a live host, and so the
    caller can swap in an SSH transport for a remote node later.
    """

    run: Callable[[list[str]], subprocess.CompletedProcess]
    observed: bool = False

    def with_log(self, log: CommandLog) -> "Command":
        if self.observed:
            return self
        def run(argv: list[str]) -> subprocess.CompletedProcess:
            started = log.begin(argv)
            before = time.monotonic()
            try:
                done = self.run(argv)
            except Exception as error:
                done = subprocess.CompletedProcess(argv, None, getattr(error, 'stdout', '') or '', getattr(error, 'stderr', '') or '')
                log.add(argv, done, started=started, duration_ms=round((time.monotonic() - before) * 1000, 2), error=f'{type(error).__name__}: {error}')
                raise
            log.add(argv, done, started=started, duration_ms=round((time.monotonic() - before) * 1000, 2))
            return done
        return Command(run=run, observed=True)

    @staticmethod
    def local(log: CommandLog | None = None) -> "Command":
        def run(argv: list[str]) -> subprocess.CompletedProcess:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            return done

        command = Command(run=run)
        return command.with_log(log) if log is not None else command


def assert_not_tailscale(target: str) -> None:
    """Refuse any target on the protected path; see core.safety.tailscale."""
    if is_tailscale_target(target):
        raise UnsafeTarget(f"{target} carries the only remote route into this network")


# ── interface: bounce a NIC that already has no carrier ──────────────────────


def read_interface(command: Command, nic: str) -> dict[str, Any]:
    """Read one NIC's operational state from the live kernel.

    ``ip -br link show`` exits 0 even for a device that does not exist — the
    complaint goes to stderr and stdout stays empty. Presence is therefore
    decided by stdout having content, never by the exit code.
    """
    assert_not_tailscale(nic)
    if not PHYSICAL_NIC.match(nic):
        return {"interface": nic, "state": "REFUSED", "carrier": False, "raw": "", "found": False,
                "note": "not a physical NIC name; bridges, veths and tunnels are out of scope"}
    result = command.run(["ip", "-br", "link", "show", nic])
    text = (result.stdout or "").strip()
    fields = text.split()
    found = bool(text)
    state = fields[1].upper() if len(fields) > 1 else ("ABSENT" if not found else "UNKNOWN")
    return {
        "interface": nic,
        "state": state,
        "carrier": state == "UP",
        "raw": text,
        "found": found,
    }


def interface_is_down(reading: dict[str, Any]) -> bool:
    """True only when the kernel says this NIC is genuinely not carrying."""
    return reading.get("found", False) and not reading.get("carrier", True)


def bounce_interface(command: Command, nic: str) -> bool:
    """Down/up a NIC that is already down. Returns whether carrier came back."""
    assert_not_tailscale(nic)
    before = read_interface(command, nic)
    if not interface_is_down(before):
        # Precondition, enforced rather than assumed: a NIC that is currently
        # carrying traffic is not a monotonic target.
        raise UnsafeTarget(f"{nic} is not down (state={before.get('state')}); refusing to bounce it")
    command.run(["ip", "link", "set", nic, "down"])
    command.run(["ip", "link", "set", nic, "up"])
    return read_interface(command, nic).get("carrier", False)


def interface_probe(command: Command, nic: str) -> HealthProbe:
    return HealthProbe(
        name=f"carrier:{nic}",
        read=lambda: read_interface(command, nic),
        healthy=lambda reading: bool(reading.get("carrier")),
        role="target",
    )


# ── systemd: restart a unit that is already failed ───────────────────────────


def read_unit(command: Command, unit: str) -> dict[str, Any]:
    """Read a unit's active state and how often it has already been restarted."""
    assert_not_tailscale(unit)
    active = command.run(["systemctl", "is-active", unit]).stdout.strip()
    restarts = command.run(["systemctl", "show", unit, "-p", "NRestarts", "--value"]).stdout.strip()
    return {
        "unit": unit,
        "active": active,
        "running": active == "active",
        "failed": active == "failed",
        "restarts": int(restarts) if restarts.isdigit() else 0,
    }


def dependency_failure(command: Command, unit: str, lines: int = 50) -> bool:
    """Did this unit die because something it depends on is unreachable?

    A restart cannot fix that, and looping restarts against a downed dependency
    is how a degradation becomes an outage. When the journal shows the
    signature, the action declines instead of trying.
    """
    result = command.run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager"])
    text = (result.stdout or "").lower()
    signatures = (
        "connection refused",
        "no route to host",
        "name or service not known",
        "temporary failure in name resolution",
        "dependency failed",
        "timed out connecting",
    )
    return any(signature in text for signature in signatures)


def restart_unit(command: Command, unit: str, *, max_restarts: int = 2) -> bool:
    """Restart a failed unit, bounded. Returns whether it came back active."""
    assert_not_tailscale(unit)
    before = read_unit(command, unit)
    if not before["failed"]:
        raise UnsafeTarget(f"{unit} is {before['active']}, not failed; refusing to restart it")
    if before["restarts"] >= max_restarts:
        raise UnsafeTarget(
            f"{unit} has already been restarted {before['restarts']} times; "
            "stopping rather than looping — this needs a person"
        )
    if dependency_failure(command, unit):
        raise UnsafeTarget(
            f"{unit} failed on an unreachable dependency; a restart cannot fix that"
        )
    command.run(["systemctl", "restart", unit])
    return read_unit(command, unit)["running"]


def unit_probe(command: Command, unit: str) -> HealthProbe:
    return HealthProbe(
        name=f"unit:{unit}",
        read=lambda: read_unit(command, unit),
        healthy=lambda reading: bool(reading.get("running")),
        role="target",
    )


# ── gateway health, as a blast-radius probe for any host action ──────────────


def gateway_probe(command: Command, url: str = "http://127.0.0.1:8026/api/healthz") -> HealthProbe:
    """Watch the console itself.

    Attached to every host action so the window notices collateral damage, not
    only whether the specific thing we touched came back. A NIC bounce that
    fixes the NIC and takes the gateway down with it must not read as a pass.
    """

    def read() -> dict[str, Any]:
        if not shutil.which("curl"):
            return {"reachable": False, "reason": "curl not available"}
        result = command.run(["curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", url])
        code = (result.stdout or "").strip()
        return {"url": url, "http_code": code, "reachable": code == "200"}

    return HealthProbe(
        name="gateway",
        read=read,
        healthy=lambda r: bool(r.get("reachable")),
        role="guard",
        failure_threshold=1,
    )
