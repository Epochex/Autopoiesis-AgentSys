"""Per-VLAN(/24) and per-node network diagnosis for the ops console.

Two clearly-separated layers, because the passive telemetry has NO timing:

  Layer 1 — PASSIVE congestion/pressure aggregate (real, from ClickHouse
  ``netops.facts``). The FortiGate syslog carries no latency/RTT/jitter and the
  facts pipeline drops ``duration``, so this layer is explicitly NOT latency: it
  is traffic volume, deny-rate and activity spread — a congestion/pressure
  signal. Mirrors ``history.py`` (same ``_q`` helper, same env, same read-only
  never-raises contract, same short TTL cache).

  Layer 2 — ACTIVE latency probe (real RTT via ICMP ``ping``, on-demand only).
  This is the only place actual round-trip time is measured, and only when a
  request explicitly asks for it.
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import threading
import time
from typing import Any

from .history import _q  # reuse the exact ClickHouse query helper / conventions

_CH_DB = os.getenv("CLICKHOUSE_DB", "netops")

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_TTL = 120

# ---------------------------------------------------------------------------
# Layer 1 — passive congestion/pressure aggregate (NOT latency)
# ---------------------------------------------------------------------------

_CIDR_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$")


def _prefix24(cidr: str) -> str | None:
    """Derive the /24 prefix ('192.168.16.') from a cidr, validating chars."""
    if not isinstance(cidr, str) or not _CIDR_RE.match(cidr.strip()):
        return None
    octets = cidr.strip().split("/")[0].split(".")
    try:
        if any(not (0 <= int(o) <= 255) for o in octets):
            return None
    except ValueError:
        return None
    return ".".join(octets[:3]) + "."


def vlan_diag(cidr: str, days: int = 7) -> dict[str, Any] | None:
    """Passive /24 congestion/pressure portrait from ClickHouse ``netops.facts``.

    NOT network latency — this is traffic volume, deny-rate and activity spread.
    Returns None if ClickHouse is unreachable (console then hides this layer).
    """
    prefix = _prefix24(cidr)
    if prefix is None:
        return None
    days = max(1, min(days, 90))
    ck = f"{prefix}:{days}"
    with _lock:
        hit = _cache.get(ck)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]

    where = f"srcip LIKE '{prefix}%' AND event_ts >= now() - INTERVAL {days} DAY"
    try:
        totals = _q(
            f"SELECT count() AS flows, sum(sentbyte) AS up, sum(rcvdbyte) AS down, "
            f"sum(sentbyte+rcvdbyte) AS bytes, countIf(action='deny') AS deny, "
            f"countIf(action='accept') AS accept, uniq(dstip) AS peers, "
            f"uniq(srcip) AS devices FROM {_CH_DB}.facts WHERE {where}"
        )
        if not totals or int(totals[0].get("flows") or 0) == 0:
            result = {"ok": True, "cidr": cidr, "days": days, "empty": True}
            with _lock:
                _cache[ck] = (time.time(), result)
            return result
        nodes = _q(
            f"SELECT srcip AS ip, count() AS flows, sum(sentbyte) AS up, "
            f"sum(rcvdbyte) AS down, countIf(action='deny') AS deny, "
            f"countIf(action='accept') AS accept, uniq(dstip) AS peers, "
            f"groupUniqArray(4)(dstport) AS topPorts "
            f"FROM {_CH_DB}.facts WHERE {where} "
            f"GROUP BY srcip ORDER BY sum(sentbyte+rcvdbyte) DESC LIMIT 60"
        )
    except Exception:
        return None

    t = totals[0]
    flows = int(t.get("flows") or 0)
    deny = int(t.get("deny") or 0)
    result = {
        "ok": True,
        "cidr": cidr,
        "days": days,
        "totals": {
            "devices": int(t.get("devices") or 0),
            "flows": flows,
            "up": int(t.get("up") or 0),
            "down": int(t.get("down") or 0),
            "bytes": int(t.get("bytes") or 0),
            "deny": deny,
            "accept": int(t.get("accept") or 0),
            "denyRate": round(deny / max(1, flows), 3),
            "peers": int(t.get("peers") or 0),
        },
        "nodes": [
            {
                "ip": n["ip"],
                "flows": int(n.get("flows") or 0),
                "up": int(n.get("up") or 0),
                "down": int(n.get("down") or 0),
                "deny": int(n.get("deny") or 0),
                "accept": int(n.get("accept") or 0),
                "denyRate": round(int(n.get("deny") or 0) / max(1, int(n.get("flows") or 0)), 3),
                "peers": int(n.get("peers") or 0),
                "topPorts": [int(p) for p in (n.get("topPorts") or []) if p],
            }
            for n in nodes
        ],
        "signal": "passive",
        "note": "被动 syslog 拥塞/压力信号(流量/deny率/活跃),非网络延迟",
    }
    with _lock:
        _cache[ck] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# Layer 2 — active ICMP latency probe (real RTT, on-demand only)
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
_RTT_RE = re.compile(
    r"rtt min/avg/max/mdev = "
    r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms"
)


def _valid_ipv4(ip: str) -> bool:
    if not isinstance(ip, str) or not _IPV4_RE.match(ip):
        return False
    try:
        return all(0 <= int(o) <= 255 for o in ip.split("."))
    except ValueError:
        return False


def _probe_one(ip: str, count: int) -> dict[str, Any]:
    """Ping a single host once; never raises. Degraded dict if ping is absent."""
    base = {
        "ip": ip,
        "reachable": False,
        "loss_pct": 100.0,
        "rtt_min": None,
        "rtt_avg": None,
        "rtt_max": None,
        "jitter": None,
    }
    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", "1", "-i", "0.2", ip],
            capture_output=True,
            text=True,
            timeout=count * 2 + 3,
        )
    except FileNotFoundError:
        base["loss_pct"] = None
        base["degraded"] = True
        base["note"] = "ping 二进制不可用"
        return base
    except Exception:
        return base

    out = proc.stdout or ""
    loss_m = _LOSS_RE.search(out)
    if loss_m:
        base["loss_pct"] = float(loss_m.group(1))
    rtt_m = _RTT_RE.search(out)
    if rtt_m:
        base["rtt_min"] = float(rtt_m.group(1))
        base["rtt_avg"] = float(rtt_m.group(2))
        base["rtt_max"] = float(rtt_m.group(3))
        base["jitter"] = float(rtt_m.group(4))
    base["reachable"] = base["loss_pct"] is not None and base["loss_pct"] < 100.0
    return base


def probe_latency(ips: list[str], count: int = 5) -> dict[str, Any]:
    """Active ICMP round-trip probe over `ips` (real RTT). Never raises.

    Runs one ``ping`` subprocess per host concurrently. Only executed on request.
    """
    count = max(1, min(int(count), 10))
    clean = [ip for ip in (ips or []) if _valid_ipv4(ip)][:32]
    results: list[dict[str, Any]] = []
    degraded = False
    if clean:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_probe_one, ip, count): ip for ip in clean}
            by_ip: dict[str, dict[str, Any]] = {}
            for fut in concurrent.futures.as_completed(futures):
                r = fut.result()
                if r.pop("degraded", False):
                    degraded = True
                r.pop("note", None)
                by_ip[r["ip"]] = r
        results = [by_ip[ip] for ip in clean if ip in by_ip]
    return {
        "ok": True,
        "probe": "icmp",
        "count": count,
        "results": results,
        "signal": "active",
        "note": "主动 ICMP 回显探测 · 实时测量(仅在请求时执行)",
        "degraded": degraded,
    }
