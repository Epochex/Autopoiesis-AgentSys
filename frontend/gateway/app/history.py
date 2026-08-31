"""Historical device portrait from ClickHouse `autopoiesis.facts`.

The live FortiGate REST API answers "what is this host doing right now"; this
answers "what has it been doing over the last N days": top destinations, daily
activity, denied-flow pressure, and bandwidth volume read from the flows ingested
off R230's full syslog (see ingest/facts_ingest.py). Read-only, short-cached.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import base64
from typing import Any

_CH_URL = os.getenv("CLICKHOUSE_URL", "http://10.43.125.243:8123")
_CH_USER = os.getenv("CLICKHOUSE_USER", "default")
_CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "")
_CH_DB = os.getenv("CLICKHOUSE_DB", "autopoiesis")

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_TTL = 120

# facts.proto stores the raw IP protocol number as a string ("6", "17", …)
_PROTO = {"1": "ICMP", "2": "IGMP", "6": "TCP", "17": "UDP", "47": "GRE", "50": "ESP", "58": "ICMPv6"}


def _q(sql: str) -> list[dict]:
    """Run a read-only ClickHouse query, return rows as dicts (JSON format)."""
    credentials = base64.b64encode(f"{_CH_USER}:{_CH_PASS}".encode()).decode()
    req = urllib.request.Request(
        f"{_CH_URL}/",
        data=(sql + " FORMAT JSON").encode("utf-8"),
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8")).get("data", [])


def _esc(ip: str) -> str:
    # IPs only ever contain [0-9.]; strip anything else defensively.
    return "".join(c for c in ip if c in "0123456789.")


def _port_row(r: dict) -> dict[str, Any]:
    """One historical (dstport, proto) aggregate, same shape as the sample-window PortUse."""
    proto = _PROTO.get(str(r.get("proto") or ""), str(r.get("proto") or "?"))
    svc = r.get("service") or None
    # drop labels that just restate port/proto ("udp/5050")
    if svc and svc.lower() == f"{proto.lower()}/{r['dstport']}":
        svc = None
    return {"port": int(r["dstport"]), "proto": proto, "service": svc,
            "hits": int(r["flows"]), "deny": int(r.get("denied") or 0)}


def available() -> bool:
    try:
        _q("SELECT 1")
        return True
    except Exception:
        return False


def device_history(ip: str, days: int = 7, full: bool = False) -> dict[str, Any] | None:
    """Historical portrait for one host over the last `days`, from ClickHouse.

    Returns None if ClickHouse is unreachable (the console then shows live-only).
    """
    ip = _esc(ip)
    if not ip:
        return None
    days = max(1, min(days, 90))
    ck = f"{ip}:{days}:{int(full)}"
    with _lock:
        hit = _cache.get(ck)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]

    where = f"srcip = '{ip}' AND event_ts >= now() - INTERVAL {days} DAY"
    try:
        totals = _q(
            f"SELECT count() AS flows, sum(sentbyte+rcvdbyte) AS bytes, "
            f"sum(sentbyte) AS up, sum(rcvdbyte) AS down, "
            f"countIf(action='deny') AS denied, uniq(dstip) AS peers, "
            f"min(event_ts) AS first, max(event_ts) AS last FROM {_CH_DB}.facts WHERE {where}"
        )
        if not totals or int(totals[0].get("flows") or 0) == 0:
            # a host can be long-offline yet fully recorded (retention starts
            # 2026-05-01): widen once to the full retained span before giving up,
            # so "last seen 13 days ago" still gets its historical portrait.
            if days < 90:
                widened = device_history(ip, 90, full)
                if widened and not widened.get("empty"):
                    widened = {**widened, "widened": True}
                    with _lock:
                        _cache[ck] = (time.time(), widened)
                    return widened
            result = {"ok": True, "ip": ip, "days": days, "flows": 0, "empty": True}
            with _lock:
                _cache[ck] = (time.time(), result)
            return result
        dest_limit = 500 if full else 12
        dests = _q(
            f"SELECT dstip, any(dstcountry) AS country, groupUniqArray(3)(service) AS services, "
            f"count() AS flows, sum(sentbyte+rcvdbyte) AS bytes, sum(sentbyte) AS up, "
            f"sum(rcvdbyte) AS down, countIf(action='deny') AS denied "
            f"FROM {_CH_DB}.facts WHERE {where} AND dstip != '' "
            f"GROUP BY dstip ORDER BY bytes DESC, flows DESC LIMIT {dest_limit}"
        )
        daily = _q(
            f"SELECT toDate(event_ts) AS d, count() AS flows, sum(sentbyte+rcvdbyte) AS bytes "
            f"FROM {_CH_DB}.facts WHERE {where} GROUP BY d ORDER BY d"
        )
        hours = _q(
            f"SELECT toHour(event_ts) AS h, count() AS flows FROM {_CH_DB}.facts "
            f"WHERE {where} GROUP BY h ORDER BY h"
        )
        top_ports = _q(
            f"SELECT dstport, proto, any(service) AS service, count() AS flows, "
            f"countIf(action='deny') AS denied FROM {_CH_DB}.facts WHERE {where} AND dstport>0 "
            f"GROUP BY dstport, proto ORDER BY flows DESC LIMIT 12"
        )
    except Exception:
        return None

    t = totals[0]
    hour_arr = [0] * 24
    for row in hours:
        hour_arr[int(row["h"])] = int(row["flows"])
    result = {
        "ok": True,
        "ip": ip,
        "days": days,
        "flows": int(t.get("flows") or 0),
        "bytes": int(t.get("bytes") or 0),
        "up": int(t.get("up") or 0),
        "down": int(t.get("down") or 0),
        "denied": int(t.get("denied") or 0),
        "peers": int(t.get("peers") or 0),
        "full": full,
        "first": t.get("first"),
        "last": t.get("last"),
        "destinations": [
            {
                "ip": d["dstip"],
                "country": d.get("country") or None,
                "services": [s for s in (d.get("services") or []) if s][:3],
                "flows": int(d["flows"]),
                "bytes": int(d["bytes"]),
                "up": int(d.get("up") or 0),
                "down": int(d.get("down") or 0),
                "denied": int(d["denied"]),
            }
            for d in dests
        ],
        "daily": [{"date": r["d"], "flows": int(r["flows"]), "bytes": int(r["bytes"])} for r in daily],
        "hours": hour_arr,
        "topPorts": [_port_row(r) for r in top_ports],
    }
    with _lock:
        _cache[ck] = (time.time(), result)
    return result
