"""What the sentinel watches for, and which safe action each finding maps to.

A detector reads the live system and returns findings. It does not decide
whether to act — that is the sentinel's job, under preconditions — but it does
declare which action, if any, a finding could map to. A detector that fires on
something with no monotonic action says so by leaving the action empty, and the
sentinel raises an alert instead of improvising one.

The mapping is deliberately narrow. Only two conditions here have an automatic
action, and both are the same shape: the target is already at its worst, so
acting cannot lower a baseline that is already on the floor.
"""

from __future__ import annotations

import re

from core.remediate.sentinel import Detection
from core.safety.tailscale import is_tailscale_target
from domains.network_rca.remediation import Command, PHYSICAL_NIC

# Units the sentinel is allowed to notice at all. An allowlist rather than
# "anything that is failed", because a failed unit somebody is mid-way through
# debugging should not be restarted underneath them.
WATCHED_UNIT_PREFIXES = ("netops-", "autopoiesis-", "demo-")


def failed_units(command: Command | None = None) -> list[Detection]:
    """Units that systemd reports as failed, restricted to the watched set."""
    command = command or Command.local()
    result = command.run(["systemctl", "--failed", "--no-legend", "--plain"])
    found: list[Detection] = []
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0]
        if is_tailscale_target(unit):
            continue
        if not unit.startswith(WATCHED_UNIT_PREFIXES):
            # Seen but not ours to touch: still reported, with no action.
            found.append(Detection(
                detector="failed_units", family="fam-perception-selfheal",
                subject=unit, severity="high",
                summary=f"{unit} 处于 failed，但不在本系统托管的单元范围内，只报不动。",
                evidence={"line": line.strip()},
            ))
            continue
        found.append(Detection(
            detector="failed_units", family="fam-perception-selfheal",
            subject=unit, severity="high",
            summary=f"{unit} 挂了。它已经不在提供服务，重启不会比现状更差。",
            evidence={"line": line.strip()},
            action="restart_unit", target=unit,
        ))
    return found


def dead_interfaces(command: Command | None = None) -> list[Detection]:
    """Physical NICs that hold an address but have lost carrier.

    A NIC with no address and no carrier is almost always just an empty port,
    which is why the address is part of the condition: an interface that was
    configured to carry something and now carries nothing is a fault, an
    interface that never carried anything is furniture.
    """
    command = command or Command.local()
    links = command.run(["ip", "-br", "link", "show"])
    addrs = command.run(["ip", "-br", "addr", "show"])

    configured: dict[str, list[str]] = {}
    for line in (addrs.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            ipv4 = [f for f in fields[2:] if re.match(r"^\d+\.\d+\.\d+\.\d+/", f)]
            if ipv4:
                configured[fields[0]] = ipv4

    found: list[Detection] = []
    for line in (links.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        nic, state = fields[0], fields[1].upper()
        if is_tailscale_target(nic) or not PHYSICAL_NIC.match(nic):
            continue
        if state == "UP" or nic not in configured:
            continue
        found.append(Detection(
            detector="dead_interfaces", family="fam-host-config-drift",
            subject=nic, severity="critical",
            summary=(
                f"{nic} 配了地址 {', '.join(configured[nic])} 但没有载波，"
                "说明它本来该承载流量、现在断了。"
            ),
            evidence={"state": state, "addresses": configured[nic]},
            action="bounce_interface", target=nic,
        ))
    return found


def admin_bruteforce(command: Command | None = None, threshold: int = 8,
                     window: str = "-10m") -> list[Detection]:
    """Repeated authentication failures against this host's own SSH.

    There is no automatic action here on purpose. Banning a source is
    reversible but banning the wrong one severs the operator's own way in, so
    this detector reports and stops — which is itself worth demonstrating.
    """
    command = command or Command.local()
    # Match on the syslog identifier rather than the unit: a real sshd sets
    # both, but auth messages relayed through syslog carry an identifier and no
    # unit, and those are exactly the ones a rehearsal or a remote relay
    # produces. Querying only by unit silently misses them.
    result = command.run([
        "journalctl", "-t", "sshd", "-t", "ssh", "--since", window, "--no-pager", "-n", "500",
    ])
    text = result.stdout or ""
    sources: dict[str, int] = {}
    for match in re.finditer(r"(?:Failed password|Invalid user|authentication failure).*?"
                             r"(?:from|rhost=)\s*(\d+\.\d+\.\d+\.\d+)", text):
        sources[match.group(1)] = sources.get(match.group(1), 0) + 1

    return [
        Detection(
            detector="admin_bruteforce", family="fam-mgmt-bruteforce",
            subject=address, severity="high",
            summary=(
                f"{address} 在最近 {window.lstrip('-')} 内对 SSH 失败登录 {count} 次。"
                "封禁是可撤销的，但封错就等于堵住自己的管理通道，所以这一条只报不动。"
            ),
            evidence={"failures": count, "window": window},
        )
        for address, count in sources.items()
        if count >= threshold and not is_tailscale_target(address)
    ]


ALL_DETECTORS = [failed_units, dead_interfaces, admin_bruteforce]
