"""Network-specific hypothesis catalogue and deterministic probe interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from core.investigate.hypothesis_loop import (
    HypothesisLoop,
    ProbeCandidate,
    RootCauseHypothesis,
)


@dataclass(frozen=True, slots=True)
class NetworkHypothesisSpec:
    statement: str
    probe: str
    terms: frozenset[str]
    auto_archivable: bool = False


ACTIVE_ROOTS: dict[str, NetworkHypothesisSpec] = {
    "carrier_down": NetworkHypothesisSpec(
        "A required physical interface has lost carrier.",
        "ip -br link show",
        frozenset({"interface", "carrier", "link", "网卡", "链路"}),
        True,
    ),
    "default_route_missing": NetworkHypothesisSpec(
        "The host has no usable default route.",
        "ip route show",
        frozenset({"route", "gateway", "network", "路由", "网关"}),
        True,
    ),
    "neighbor_unreachable": NetworkHypothesisSpec(
        "The named neighbour cannot be resolved on the local link.",
        "ip neigh show",
        frozenset({"neighbor", "arp", "address", "邻居", "地址"}),
    ),
    "service_failed": NetworkHypothesisSpec(
        "A required systemd service is in failed state.",
        "systemctl --failed --no-legend",
        frozenset({"service", "systemd", "failed", "服务"}),
        True,
    ),
    "disk_pressure": NetworkHypothesisSpec(
        "A writable host filesystem is at or above 90 percent use.",
        "df -h",
        frozenset({"disk", "filesystem", "storage", "full", "磁盘"}),
        True,
    ),
    "memory_pressure": NetworkHypothesisSpec(
        "Available host memory is at or below 10 percent.",
        "free -m",
        frozenset({"memory", "ram", "内存"}),
        True,
    ),
    "healthcheck_failed": NetworkHypothesisSpec(
        "The local investigation service health endpoint is failing.",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
        frozenset({"health", "gateway", "api", "健康"}),
    ),
    "system_errors": NetworkHypothesisSpec(
        "The current host journal contains recent error-priority events.",
        "journalctl -p err -n 40 --no-pager --since -24h",
        frozenset({"journal", "log", "error", "日志", "错误"}),
    ),
    "kernel_errors": NetworkHypothesisSpec(
        "The kernel reports a current error, critical, or alert event.",
        "dmesg -T --level err,crit,alert -x",
        frozenset({"kernel", "dmesg", "error", "内核", "错误"}),
    ),
}

FAMILY_ACTIVE_ROOTS: dict[str, tuple[str, ...]] = {
    "fam-host-config-drift": (
        "carrier_down", "default_route_missing", "neighbor_unreachable",
    ),
    "fam-perception-selfheal": (
        "service_failed", "disk_pressure", "memory_pressure", "healthcheck_failed",
    ),
    "fam-address-ownership": ("neighbor_unreachable",),
    "fam-policy-reachability": (
        "carrier_down", "default_route_missing", "neighbor_unreachable",
    ),
}

EXPECTED_DOWN_INTERFACES = frozenset({"eth0", "eth1", "eth3", "eth4", "eth5", "idrac"})
AUTO_ARCHIVABLE_ROOTS = frozenset(
    root_id for root_id, spec in ACTIVE_ROOTS.items() if spec.auto_archivable
)


def command_matches_probe(command: str, probe: str) -> bool:
    command = command.strip()
    probe = probe.strip()
    if command == probe:
        return True
    return any(
        command.startswith(prefix) and probe.startswith(prefix)
        for prefix in ("ip route show", "df -h")
    )


def create_network_hypothesis_loop(
    *,
    case_id: str,
    family: str | None,
    subject: str | None,
    opened_at: datetime,
    question_terms: Sequence[str],
    ordered_commands: Sequence[str],
) -> HypothesisLoop:
    """Create the applicable candidate set and rank its read-only probes."""
    root_ids = FAMILY_ACTIVE_ROOTS.get(family, ()) if family else tuple(ACTIVE_ROOTS)
    if not subject or re.fullmatch(r"\d+(?:\.\d+){3}", subject) is None:
        root_ids = tuple(root_id for root_id in root_ids if root_id != "neighbor_unreachable")
    loop = HypothesisLoop.create(case_id, at=opened_at)
    entity = subject or "local-system"
    terms = set(question_terms)
    order = {
        root_id: next(
            (
                index for index, command in enumerate(ordered_commands)
                if command_matches_probe(command, ACTIVE_ROOTS[root_id].probe)
            ),
            len(ordered_commands) + list(ACTIVE_ROOTS).index(root_id),
        )
        for root_id in root_ids
    }
    for root_id in root_ids:
        spec = ACTIVE_ROOTS[root_id]
        loop.add_hypothesis(RootCauseHypothesis(
            hypothesis_id=root_id,
            statement=spec.statement,
            entity_id=entity,
            valid_from=opened_at,
            valid_to=opened_at + timedelta(days=30),
            updated_at=opened_at,
            origin="catalog",
            archive_eligible=spec.auto_archivable,
        ))
        overlap = len(terms.intersection(spec.terms))
        loop.add_probe(ProbeCandidate(
            probe_id=f"probe:{root_id}",
            description=spec.probe,
            target_entity_id=entity,
            distinguishes_hypothesis_ids=(root_id,),
            priority=10_000 - order[root_id] * 100 + overlap * 1_000,
            estimated_cost=1.0,
        ), at=opened_at)
    return loop


def probe_observation(
    root_id: str,
    item: Mapping[str, object],
    subject: str | None,
) -> tuple[str, bool, str]:
    """Map a fresh command result to polarity, decisiveness and collection state."""
    if not item.get("ok"):
        return "neutral", False, "tool_failed"
    output = str(item.get("output") or "")

    if root_id == "carrier_down":
        down = False
        for line in output.splitlines():
            columns = line.split()
            if not columns:
                continue
            interface = columns[0].split("@")[0]
            physical = re.fullmatch(r"(?:eth|eno|enp|ens)\w+", interface) is not None
            targeted = (
                subject == interface
                if subject and re.fullmatch(r"\d+(?:\.\d+){3}", subject) is None
                else True
            )
            if (
                physical
                and targeted
                and interface not in EXPECTED_DOWN_INTERFACES
                and ("NO-CARRIER" in line.upper() or re.search(r"\bDOWN\b", line.upper()))
            ):
                down = True
                break
        return ("supports" if down else "opposes"), True, "observed"
    if root_id == "default_route_missing":
        has_default = any(line.lstrip().startswith("default ") for line in output.splitlines())
        return ("opposes" if has_default else "supports"), True, "observed"
    if root_id == "neighbor_unreachable":
        if not subject or re.fullmatch(r"\d+(?:\.\d+){3}", subject) is None:
            return "neutral", False, "observed"
        failed = any(
            subject in line and re.search(r"\bFAILED\b", line)
            for line in output.splitlines()
        )
        return ("supports" if failed else "opposes"), True, "observed"
    if root_id == "service_failed":
        failed = bool(output.strip())
        if subject and subject.endswith((".service", ".target", ".socket", ".timer")):
            failed = subject in output
        return ("supports" if failed else "opposes"), True, "observed"
    if root_id == "disk_pressure":
        pressured = False
        for line in output.splitlines()[1:]:
            columns = line.split()
            if len(columns) < 2 or re.fullmatch(r"\d{1,3}%", columns[-2]) is None:
                continue
            filesystem, mountpoint = columns[0], columns[-1]
            pseudo = (
                filesystem.startswith("/dev/loop")
                or filesystem in {"tmpfs", "devtmpfs", "squashfs"}
                or mountpoint.startswith(("/snap/", "/proc/", "/sys/", "/run/"))
            )
            if not pseudo and int(columns[-2][:-1]) >= 90:
                pressured = True
                break
        return ("supports" if pressured else "opposes"), True, "observed"
    if root_id == "memory_pressure":
        line = next(
            (row for row in output.splitlines() if row.lstrip().startswith("Mem:")),
            "",
        )
        numbers = [int(value) for value in re.findall(r"\d+", line)]
        pressured = bool(
            len(numbers) >= 2 and numbers[0] > 0 and numbers[-1] / numbers[0] <= 0.10
        )
        return ("supports" if pressured else "opposes"), bool(numbers), "observed"
    if root_id == "healthcheck_failed":
        match = re.fullmatch(r"\s*(\d{3})\s*", output)
        if match is None:
            return "neutral", False, "observed"
        failed = not 200 <= int(match.group(1)) < 300
        return ("supports" if failed else "opposes"), True, "observed"
    if root_id == "system_errors":
        has_error = bool(output.strip() and "-- No entries --" not in output)
        return ("supports" if has_error else "opposes"), True, "observed"
    if root_id == "kernel_errors":
        return ("supports" if output.strip() else "opposes"), True, "observed"
    return "neutral", False, "observed"


__all__ = [
    "ACTIVE_ROOTS",
    "AUTO_ARCHIVABLE_ROOTS",
    "EXPECTED_DOWN_INTERFACES",
    "FAMILY_ACTIVE_ROOTS",
    "NetworkHypothesisSpec",
    "command_matches_probe",
    "create_network_hypothesis_loop",
    "probe_observation",
]
