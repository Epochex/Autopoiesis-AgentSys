"""Differential ownership probe: prove a duplicate address by what answers on it.

A contested address passes ping from either claimant, because ICMP is stateless
and both devices answer for the address they both believe they own. That makes
reachability tests useless here -- the naive check reports 0% loss while every
stateful connection to the address is landing on a coin flip.

What separates the claimants is the *service profile*. Two different machines
expose different ports, so sampling a small port set while recording which MAC
currently owns the ARP entry produces a table where the open-port set changes
with the owner. That table is the proof, and it also quantifies the damage:
the fraction of samples in which the intended service was unreachable is the
fraction of connection attempts that go to the wrong machine.

Read-only by construction: a TCP connect that is closed immediately, plus a
read of the local neighbour table. No writes, no auth attempts, no payload.
"""
from __future__ import annotations

import socket
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

# Ports are allowlisted, not caller-supplied: this runs against production LAN
# equipment, and "probe any port you like" is a port scanner with a REST API.
ALLOWED_PORTS: dict[int, str] = {
    22: "ssh",
    80: "http",
    443: "https",
    2026: "ops-console",
    3389: "rdp",
    5432: "postgres",
    6443: "kube-apiserver",
    8123: "clickhouse-http",
    9092: "kafka/redpanda",
    10250: "kubelet",
    37777: "dahua-sdk",
    554: "rtsp",
}
DEFAULT_PORTS = (22, 10250, 37777)

MAX_SAMPLES = 40
MAX_PORTS = 6
CONNECT_TIMEOUT = 1.0


def _neighbour_mac(ip: str, timeout: float = 3.0) -> str | None:
    """Current L2 owner of the address from the local neighbour table."""
    try:
        result = subprocess.run(
            ["ip", "neigh", "show", ip], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if "lladdr" not in result.stdout:
        return None
    return result.stdout.split("lladdr", 1)[1].split()[0].strip().lower()


def _tcp_state(ip: str, port: int, timeout: float = CONNECT_TIMEOUT) -> str:
    """One read-only TCP connect. ``open`` / ``refused`` / ``timeout`` / ``error``."""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return "open"
    except socket.timeout:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "error"
    finally:
        sock.close()


def probe_address_ownership(
    ip: str,
    *,
    ports: tuple[int, ...] | list[int] = DEFAULT_PORTS,
    samples: int = 20,
    interval: float = 1.0,
) -> dict[str, Any]:
    """Sample (ARP owner, open-port set) pairs and report the split.

    ``verdict`` is ``contested`` only when more than one MAC answered *and* the
    service profiles differ between them. One MAC, or identical profiles, is
    reported as inconclusive rather than as a clean result: this probe watches a
    short window, and not catching a handover is not the same as there not being
    one.
    """
    selected = [int(port) for port in ports if int(port) in ALLOWED_PORTS][:MAX_PORTS]
    if not selected:
        raise ValueError("no allowed ports selected")
    count = max(2, min(int(samples), MAX_SAMPLES))

    started = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        owner = _neighbour_mac(ip)
        states = {port: _tcp_state(ip, port) for port in selected}
        rows.append(
            {
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "owner": owner,
                "ports": {str(port): state for port, state in states.items()},
            }
        )
        if index < count - 1:
            time.sleep(interval)

    by_owner: dict[str, Counter[str]] = defaultdict(Counter)
    owner_samples: Counter[str] = Counter()
    for row in rows:
        owner = row["owner"] or "unresolved"
        owner_samples[owner] += 1
        for port, state in row["ports"].items():
            if state == "open":
                by_owner[owner][port] += 1

    profiles = {
        owner: sorted(port for port in by_owner[owner] if by_owner[owner][port] > 0)
        for owner in owner_samples
    }
    resolved_owners = [owner for owner in owner_samples if owner != "unresolved"]
    distinct_profiles = {tuple(profiles[owner]) for owner in resolved_owners}
    contested = len(resolved_owners) > 1 and len(distinct_profiles) > 1

    # Services that only one claimant serves are unreachable whenever the other
    # one holds the address. That share is the real availability cost.
    impact: dict[str, Any] = {}
    total = sum(owner_samples[owner] for owner in resolved_owners)
    for port in map(str, selected):
        serving = [owner for owner in resolved_owners if port in profiles[owner]]
        if serving and len(serving) < len(resolved_owners) and total:
            reachable = sum(owner_samples[owner] for owner in serving)
            impact[port] = {
                "service": ALLOWED_PORTS[int(port)],
                "served_by": serving,
                "unreachable_share": round(1 - reachable / total, 3),
            }

    return {
        "ip": ip,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "samples": count,
        "ports": {str(port): ALLOWED_PORTS[port] for port in selected},
        "owner_samples": dict(owner_samples),
        "service_profiles": profiles,
        "verdict": "contested" if contested else "inconclusive",
        "verdict_note": (
            "two MACs answered for this address with different service profiles: "
            "connections land on a different machine depending on which one won the last ARP exchange"
            if contested
            else "a single owner answered throughout this window -- not proof the address is uncontested, "
            "only that no handover was caught in it"
        ),
        "impact": impact,
        "rows": rows,
        "read_only": True,
    }
