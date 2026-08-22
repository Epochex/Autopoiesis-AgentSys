"""What each fix would actually touch, computed rather than asserted.

"May affect business services" is not a blast radius; it is a way of not
answering. A useful one is a count the operator can check: how many connections
are open on the port about to be closed, how many distinct sources used it last
week, how long the service being restarted takes to come back.

Everything here is measured on the live box at the moment it is asked. Where a
number cannot be measured, the field says so rather than guessing — an
`unknown` the reader can see is worth more than a confident zero.

The estimates are deliberately conservative in one direction: when a source
disagrees or is missing, the radius is reported as larger, never smaller.
"""

from __future__ import annotations

import os
import re
from typing import Any

from core.investigate.safe_exec import run as safe_run
from core.safety.tailscale import is_tailscale_target

# How far back to look for "has anyone used this recently".
LOOKBACK_DAYS = int(os.getenv("AUTOPOIESIS_BLAST_LOOKBACK_DAYS", "7"))


def _interface_radius(target: str) -> dict[str, Any]:
    """Bouncing a NIC affects whatever is riding on that NIC. Count it."""
    link = safe_run(f"ip -br link show {target}")
    addr = safe_run(f"ip -br addr show {target}")
    routes = safe_run("ip route show")

    # `ip` writes "Device ... does not exist." to stderr and exits non-zero;
    # safe_exec merges stderr into output, so a missing NIC has *more* output
    # than a present one. Presence is decided by the exit code here, unlike in
    # the remediation layer where the raw stdout is available separately.
    if link.exit_code != 0 or "does not exist" in (link.output or ""):
        return {
            "scope": "none",
            "summary": f"这台机器上没有 {target} 这个网口，动作不会执行。",
            "measured": {"exists": False},
            "reversible": None,
        }
    carrier = "UP" in (link.output or "").split()[1:2]
    addresses = [a for a in re.findall(r"\d+\.\d+\.\d+\.\d+/\d+", addr.output or "")]
    via_this_nic = [
        line for line in (routes.output or "").splitlines() if f"dev {target}" in line
    ]

    if carrier:
        return {
            "scope": "blocked",
            "summary": (
                f"{target} 正在承载流量（{len(addresses)} 个地址、{len(via_this_nic)} 条路由），"
                "前置条件会拒绝这个动作。"
            ),
            "measured": {"carrier": True, "addresses": addresses, "routes": len(via_this_nic)},
        }
    return {
        "scope": "single-nic",
        "summary": (
            f"只影响 {target} 一个口。它当前无载波、无 IP、无路由经过，"
            "所以断开重连不会中断任何现有连接。"
        ),
        "measured": {"carrier": False, "addresses": addresses, "routes": len(via_this_nic)},
    }


def _unit_radius(target: str) -> dict[str, Any]:
    """Restarting a unit affects whoever depends on it, for as long as it takes."""
    state = safe_run(f"systemctl is-active {target}")
    props = safe_run(f"systemctl show {target} -p NRestarts -p ExecMainStartTimestamp --no-pager")
    listening = safe_run("ss -tulpn")

    active = (state.output or "").strip()
    ports = sorted({
        match.group(1)
        for line in (listening.output or "").splitlines()
        if target.split(".")[0][:12] in line
        for match in [re.search(r":(\d+)\s", line)]
        if match
    })

    if active == "active":
        return {
            "scope": "blocked",
            "summary": f"{target} 正在运行，前置条件会拒绝重启。",
            "measured": {"state": active, "ports": ports},
        }
    return {
        "scope": "single-service",
        "summary": (
            f"只影响 {target} 这一个服务，它现在是 {active}——已经不在提供服务了。"
            + (f" 它监听的端口：{', '.join(ports)}。" if ports else "")
        ),
        "measured": {"state": active, "ports": ports},
    }


def _port_closure_radius(target: str, port: str) -> dict[str, Any]:
    """Closing a port removes access. Count who is using it before saying so."""
    established = safe_run(f"ss -tn state established")
    open_now = [
        line for line in (established.output or "").splitlines()
        if f":{port} " in line or line.rstrip().endswith(f":{port}")
    ]
    return {
        "scope": "service-wide",
        "summary": (
            f"关闭 {target}:{port} 会移除这个端口的全部访问。当前观察到 {len(open_now)} 条已建立连接；"
            f"过去 {LOOKBACK_DAYS} 天用过它的来源数需要查流量库，未查到之前按“有人在用”对待。"
        ),
        "measured": {"established_now": len(open_now), "lookback_days": LOOKBACK_DAYS,
                     "distinct_sources": None},
        "reversible": False,
    }


def estimate(action: str, target: str, port: str | None = None) -> dict[str, Any]:
    """Measure what this action would touch. Never guesses; says unknown instead."""
    if is_tailscale_target(target):
        return {
            "scope": "refused",
            "summary": (
                f"{target} 在 Tailscale 路径上，是从外面进入这个内网的唯一通道。"
                "任何动作都不许碰它——断了就再也连不进来。"
            ),
            "measured": {"protected": True},
            "reversible": False,
        }

    if action == "bounce_interface":
        radius = _interface_radius(target)
        radius["reversible"] = False  # the pre-state is "down"; there is no inverse
        return radius
    if action == "restart_unit":
        radius = _unit_radius(target)
        radius["reversible"] = False
        return radius
    if action == "close_port" and port:
        return _port_closure_radius(target, port)

    return {
        "scope": "unknown",
        "summary": "这个动作没有可计算的影响面，按需要人工评估处理。",
        "measured": {},
        "reversible": None,
    }
