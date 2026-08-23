"""Learn dated host facts from the sentinel's read-only observations.

These ``asset_profile`` facts answer only "where should I look first?" and
"what did this host look like when it was observed?"  They are historical
context, never action evidence.  ``blast_radius.estimate()``, remediation
preconditions, and every other preflight path must measure the live host and
must never read this module's memory records.  A stale plan is unsafe even when
the observation that originally produced it was exact.

Parsing and selection are deterministic.  The command output is the evidence;
no model decides whether a reading is normal or whether an action is safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Protocol, Sequence

from core.evolve import memory_ops
from core.memory.store import TieredMemoryStore


class _CommandResult(Protocol):
    stdout: str | None
    returncode: int


class EnvironmentCommand(Protocol):
    """The read-only command shape already used by network RCA detectors."""

    def run(self, argv: list[str]) -> _CommandResult: ...


_PHYSICAL_INTERFACE = re.compile(r"^(?:eno|enp|eth|ens)\d[A-Za-z0-9_.-]*$")
_IPV4_CIDR = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}/(?:[12]?\d|3[0-2])$"
)


def _instant(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _interface_name(raw: str) -> str:
    # ``ip`` prints veth peers as veth123@if7.  The suffix identifies the peer's
    # namespace index, not a second host asset, and changes when containers do.
    return raw.split("@", 1)[0]


def _link_state(fields: Sequence[str]) -> str:
    state = fields[1].casefold().replace("_", "-")
    flags = " ".join(fields[2:]).upper()
    if state in {"no-carrier", "lowerlayerdown"} or "NO-CARRIER" in flags:
        return "no-carrier"
    return state


def _parse_links(output: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            links[_interface_name(fields[0])] = _link_state(fields)
    return links


def _parse_addresses(output: str) -> dict[str, str]:
    addresses: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        interface = _interface_name(fields[0])
        # One keyed fact can have one current value.  Prefer the first global or
        # RFC1918 IPv4 emitted by ``ip`` and leave IPv6/link-local detail to live
        # diagnostics, so multiple addresses never supersede one another inside
        # a single observation.
        addresses[interface] = next(
            (field for field in fields[2:] if _IPV4_CIDR.fullmatch(field)),
            "none",
        )
    return addresses


def _interface_kind(interface: str) -> str | None:
    if interface == "docker0" or interface.startswith("br-"):
        return "container_bridge"
    if interface.startswith("veth"):
        return "container_peer"
    if interface == "flannel.1":
        return "k3s_overlay"
    if interface == "cni0":
        return "k3s_bridge"
    return None


def _failed_units(output: str) -> str:
    units: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if fields and (fields[0].endswith(".service") or fields[0].endswith(".target")):
            units.add(fields[0])
    # The complete result is one snapshot-valued fact.  An empty listing can
    # therefore supersede yesterday's failures instead of leaving them current.
    return ",".join(sorted(units)) if units else "none"


def _facts(
    link_output: str | None,
    address_output: str | None,
    failed_output: str | None,
) -> list[tuple[str, str, str]]:
    links = _parse_links(link_output) if link_output is not None else {}
    addresses = _parse_addresses(address_output) if address_output is not None else {}
    facts: list[tuple[str, str, str]] = []

    for interface, state in links.items():
        facts.append((interface, "link_state", state))
        kind = _interface_kind(interface)
        if kind is not None:
            facts.append((interface, "interface_kind", kind))

    for interface, address in addresses.items():
        facts.append((interface, "address", address))

    primary = [
        interface
        for interface, state in links.items()
        if state == "up"
        and _PHYSICAL_INTERFACE.fullmatch(interface)
        and addresses.get(interface, "none") != "none"
    ]
    if len(primary) == 1:
        # Uniqueness is important: two configured uplinks do not reveal which
        # route is preferred.  In that case a routing probe must settle it.
        facts.append(("this-host", "primary_interface", primary[0]))

    if failed_output is not None:
        facts.append(("this-host", "failed_units", _failed_units(failed_output)))
    return facts


def _successful_output(result: _CommandResult) -> str | None:
    # Empty successful output is meaningful for ``systemctl --failed``.  Empty
    # output from a failed probe is no observation and must not revoke a fact.
    if getattr(result, "returncode", 0) != 0:
        return None
    return result.stdout or ""


def observe_environment(
    command: EnvironmentCommand,
    memory: TieredMemoryStore,
    *,
    now: datetime | None = None,
    recorder: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Observe one host snapshot and return the memory ids holding its facts.

    ``observe_fact`` owns keyed deduplication, validity intervals, retirement of
    a changed value, and observability events.  Keeping those lifecycle rules in
    one place is what makes a 15-second polling loop safe to replay.
    """
    observed_at = _instant(now)
    links = command.run(["ip", "-br", "link", "show"])
    addresses = command.run(["ip", "-br", "addr", "show"])
    failures = command.run(["systemctl", "--failed", "--no-legend", "--plain"])

    observe_fact = getattr(memory_ops, "observe_fact", None)
    if not callable(observe_fact):
        raise RuntimeError("core.evolve.memory_ops.observe_fact is not available")

    held: list[str] = []
    for subject, relation, value in _facts(
        _successful_output(links),
        _successful_output(addresses),
        _successful_output(failures),
    ):
        event_offset = len(recorder) if recorder is not None else 0
        memory_id = observe_fact(
            memory,
            subject=subject,
            relation=relation,
            value=value,
            observed_at=observed_at,
            recorder=recorder,
        )
        record = memory.get(memory_id)
        if record is not None:
            # Key lifecycle is generic, while these host-shape facts belong to
            # the highest-prior asset profile.  The generic writer deliberately
            # does not choose a domain tier for its callers.
            record.tier = "asset_profile"
        if recorder is not None:
            for event in recorder[event_offset:]:
                if event.get("memory_id") == memory_id:
                    event["tier"] = "asset_profile"
        held.append(memory_id)
    return held
