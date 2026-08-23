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
from ipaddress import ip_address, ip_network

from core.remediate.sentinel import Detection
from core.safety.tailscale import is_tailscale_target
from domains.network_rca.remediation import Command, PHYSICAL_NIC

# Units the sentinel is allowed to notice at all. An allowlist rather than
# "anything that is failed", because a failed unit somebody is mid-way through
# debugging should not be restarted underneath them.
WATCHED_UNIT_PREFIXES = ("netops-", "autopoiesis-", "demo-")

_DOCUMENTATION_NETWORKS = tuple(ip_network(cidr) for cidr in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
))


def _bruteforce_safety_reason(address: str) -> str:
    """Explain the exact firewall-write boundary for this source."""
    parsed = ip_address(address)
    if any(parsed in network for network in _DOCUMENTATION_NETWORKS):
        return (
            f"保留未执行临时防火墙封禁：{address} 属于 RFC 5737 演示保留地址，"
            "本次只有注入的失败登录日志，没有可阻断的真实连接。写入真实防火墙会制造无效 ACL，"
            "并引入管理通道误封风险；系统保留证据并转人工。"
        )
    return (
        "保留未执行临时防火墙封禁：当前未注册同时具备封禁 TTL、管理地址豁免、"
        "提交后回读和超时自动回滚的防火墙适配器。直接写 ACL 可能误封管理来源；"
        "系统保留证据并转人工。"
    )


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
                candidate_action="restart_unit",
                safety_reason=(
                    f"保留未执行单元重启：{unit} 不在受控单元白名单内；"
                    "系统保留证据并转人工。"
                ),
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
                "安全门已评估临时封禁，具体保留理由记录在处置链。"
            ),
            evidence={"failures": count, "window": window},
            candidate_action="temporary_firewall_block",
            safety_reason=_bruteforce_safety_reason(address),
        )
        for address, count in sources.items()
        if count >= threshold and not is_tailscale_target(address)
    ]


ALL_DETECTORS = [failed_units, dead_interfaces, admin_bruteforce]
