"""READ-ONLY FortiGate identity enrichment.

The mined device graph knows a host only as well as the syslog sample does, so
half the segment renders as "unknown". The FortiGate itself keeps a live device
inventory (hostname / OS / hardware vendor per MAC, from DHCP + traffic
fingerprinting). This module pulls that inventory over the REST API and merges
it into the console's device records.

STRICTLY read-only by construction: every data request goes through `_monitor_get`,
which refuses any path outside /api/v2/monitor/ — the /api/v2/cmdb configuration
tree is unreachable from here, so no code path can alter the router.

Credentials come from the gateway environment (never committed):
  FGT_BASE       default https://192.168.1.1
  FGT_USER / FGT_PASS
  FGT_CRED_FILE  optional file with `user=...` / `pass=...` lines, used as fallback
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

_TTL_SEC = 600
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "byIp": {}, "byMac": {}}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE  # LAN router with a self-signed cert


def _creds() -> tuple[str, str, str] | None:
    base = os.getenv("FGT_BASE", "https://192.168.1.1").rstrip("/")
    user = os.getenv("FGT_USER", "")
    pw = os.getenv("FGT_PASS", "")
    path = os.getenv("FGT_CRED_FILE", "")
    if (not user or not pw) and path and os.path.exists(path):
        try:
            text = open(path, encoding="utf-8").read()
            mu = re.search(r"(?im)^\s*user(?:name)?\s*[:=]\s*(\S+)", text)
            mp = re.search(r"(?im)^\s*pass(?:word)?\s*[:=]\s*(\S+)", text)
            mb = re.search(r"(?im)^\s*(?:base|host|url)\s*[:=]\s*(\S+)", text)
            user = user or (mu.group(1) if mu else "")
            pw = pw or (mp.group(1) if mp else "")
            if mb:
                base = mb.group(1).rstrip("/")
                if not base.startswith("http"):
                    base = f"https://{base}"
        except Exception:
            pass
    return (base, user, pw) if user and pw else None


class _Session:
    """Cookie-auth FortiOS session that can only reach monitor (read) endpoints."""

    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_CTX),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.csrf = ""

    def login(self, user: str, pw: str) -> bool:
        body = urllib.parse.urlencode({"username": user, "secretkey": pw}).encode()
        req = urllib.request.Request(f"{self.base}/logincheck", data=body, method="POST")
        with self.opener.open(req, timeout=6) as r:
            ok = r.status == 200
        for c in self.jar:
            if c.name.startswith("ccsrftoken") and c.value:
                self.csrf = c.value.strip('"')
        return ok and bool(self.csrf)

    def monitor_get(self, path: str) -> Any:
        # The read-only guarantee lives on this single choke point.
        if not path.startswith("/api/v2/monitor/"):
            raise ValueError(f"refusing non-monitor path: {path}")
        req = urllib.request.Request(f"{self.base}{path}", headers={"X-CSRFTOKEN": self.csrf})
        with self.opener.open(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))

    def logout(self) -> None:
        try:
            req = urllib.request.Request(f"{self.base}/logout", data=b"", method="POST",
                                         headers={"X-CSRFTOKEN": self.csrf})
            self.opener.open(req, timeout=4).close()
        except Exception:
            pass


def _nested(v: Any) -> str | None:
    """FortiOS 6.2 nests fingerprinted attrs as {"name": ..., "src": "dhcp|http"}."""
    if isinstance(v, dict):
        return v.get("name") or None
    return v or None


def _norm(rec: dict) -> dict[str, Any]:
    return {
        "hostname": _nested(rec.get("host")) or rec.get("hostname"),
        "os": _nested(rec.get("os")),
        "osVersion": rec.get("software_version") or None,
        "vendor": rec.get("hardware_vendor") or None,
        "type": rec.get("hardware_type") or None,
        "family": rec.get("hardware_family") or None,
        "mac": (rec.get("mac") or "").lower() or None,
        "lastSeen": rec.get("last_seen"),
        "source": "fortigate-live",
    }


# FortiGate hardware_type → the roles the console's map already speaks.
_TYPE_ROLE = {
    "phone": "mobile", "mobile generic": "mobile", "tablet": "mobile",
    "camera": "camera", "ip camera": "camera",
    "server": "server", "nas": "server",
    "pc": "workstation", "computer": "workstation", "desktop": "workstation", "laptop": "workstation",
}


def _fetch() -> tuple[dict[str, Any], dict[str, Any]]:
    creds = _creds()
    if not creds:
        return {}, {}
    base, user, pw = creds
    sess = _Session(base)
    by_ip: dict[str, Any] = {}
    by_mac: dict[str, Any] = {}
    try:
        if not sess.login(user, pw):
            return {}, {}
        # FortiOS moved the inventory endpoint across releases; take the first that
        # answers (6.2.7 = device/select; the others cover newer firmware).
        for path in (
            "/api/v2/monitor/user/device/select",
            "/api/v2/monitor/user/device/query?number=2000",
            "/api/v2/monitor/user/detected-device",
        ):
            try:
                data = sess.monitor_get(path)
            except Exception:
                continue
            rows = data.get("results") or []
            if isinstance(rows, dict):
                rows = list(rows.values())
            for rec in rows:
                if not isinstance(rec, dict):
                    continue
                info = _norm(rec)
                ip = rec.get("addr") or rec.get("ipv4_address") or rec.get("ip") or rec.get("last_ip")
                if ip:
                    by_ip[ip] = info
                if info["mac"]:
                    by_mac[info["mac"]] = info
            if by_ip or by_mac:
                break
    except Exception:
        return {}, {}
    finally:
        sess.logout()
    return by_ip, by_mac


def identity_map(refresh: bool = False) -> dict[str, Any]:
    """{byIp, byMac, fresh} from the router's live inventory; {} maps when unreachable."""
    with _lock:
        age = time.time() - _cache["at"]
        if not refresh and age < _TTL_SEC and (_cache["byIp"] or _cache["byMac"]):
            return {"byIp": _cache["byIp"], "byMac": _cache["byMac"], "fresh": True}
    by_ip, by_mac = _fetch()
    with _lock:
        if by_ip or by_mac:
            _cache.update(at=time.time(), byIp=by_ip, byMac=by_mac)
        return {
            "byIp": _cache["byIp"],
            "byMac": _cache["byMac"],
            "fresh": bool(by_ip or by_mac),
        }


def enrich_device(dev: dict[str, Any]) -> dict[str, Any]:
    """Overlay the FortiGate's live device fingerprint onto a mined host record.

    Match by MAC first (DHCP reassigns IPs, so an IP match can attach the wrong
    device), then fall back to IP. For IDENTITY fields (hostname / vendor / OS /
    role) the router's active DHCP+HTTP fingerprint is authoritative and wins over
    the syslog OUI guess — which is unreliable for the many phones that randomize
    their MAC. The MEASURED mined facts (flows, deny, edges) are never touched.
    """
    ids = identity_map()
    mac = (dev.get("mac") or "").lower()
    info = (ids["byMac"].get(mac) if mac else None) or ids["byIp"].get(dev.get("ip"))
    if not info:
        return dev
    out = dict(dev)
    if info.get("hostname"):
        out["name"] = info["hostname"]
    if info.get("vendor"):
        out["vendor"] = info["vendor"]
    elif not out.get("vendor"):
        out["vendor"] = "unknown"
    if info.get("os"):
        out["os"] = info["os"]
    role = _TYPE_ROLE.get(str(info.get("type") or "").lower())
    if role:
        out["role"] = role
    out["liveIdentity"] = info
    return out


# ── live behavior: the router's CURRENT view of one host ─────────────────────
_flow_lock = threading.Lock()
_sess_cache: dict[str, Any] = {"at": 0.0, "rows": []}
# rolling snapshot for rate computation: {(saddr,daddr,dport): cumulative_bytes}
_rate_prev: dict[str, Any] = {"at": 0.0, "bytes": {}}

_SHAPER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kmg]?)bps", re.I)


def _shaper_bps(name: str | None) -> int | None:
    """Parse a FortiOS shaper label like 'guarantee-100kbps' → bits/s cap."""
    if not name:
        return None
    m = _SHAPER_RE.search(name)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"k": 1e3, "m": 1e6, "g": 1e9, "": 1}[m.group(2).lower()]
    return int(val * mult)


def _pair_bytes(rows: list[dict]) -> dict[tuple, int]:
    agg: dict[tuple, int] = {}
    for r in rows:
        k = (r.get("saddr"), r.get("daddr"), r.get("dport"))
        agg[k] = agg.get(k, 0) + (r.get("sentbyte") or 0) + (r.get("rcvdbyte") or 0)
    return agg


def live_bandwidth() -> dict[tuple, dict[str, Any]]:
    """Current per-(src,dst,port) bandwidth from two session-table snapshots.

    Rate = Δbytes / Δt against a rolling previous snapshot (2–30s old); when no
    usable baseline exists, takes a fresh 2s pair so a first click still measures.
    Returns {(saddr,daddr,dport): {bps, limitBps}} — limitBps is the configured
    shaper cap (the only "max" FortiOS exposes here; physical link speed reads 0).
    """
    # Always sample FRESH for the current reading — the 20s display cache would
    # otherwise hand back the same bytes as the baseline and every delta be zero.
    rows = _session_table_fresh()
    now_bytes = _pair_bytes(rows)
    limits: dict[tuple, int | None] = {}
    for r in rows:
        k = (r.get("saddr"), r.get("daddr"), r.get("dport"))
        limits.setdefault(k, _shaper_bps(r.get("shaper")) or _shaper_bps(r.get("reverse_shaper")))

    with _flow_lock:
        prev, prev_at = dict(_rate_prev["bytes"]), _rate_prev["at"]
    dt = time.time() - prev_at
    if not prev or dt < 1.5 or dt > 30:
        # cold baseline → take a second fresh sample after a short gap
        time.sleep(2.0)
        later = _pair_bytes(_session_table_fresh())
        base, new, dt = now_bytes, later, 2.0
    else:
        base, new = prev, now_bytes

    out: dict[tuple, dict[str, Any]] = {}
    for k, b in new.items():
        delta = b - base.get(k, b)
        bps = int(delta * 8 / dt) if delta > 0 else 0
        out[k] = {"bps": bps, "limitBps": limits.get(k)}
    with _flow_lock:
        _rate_prev.update(at=time.time(), bytes=new)
    return out


def _session_table_fresh() -> list[dict]:
    """One uncached pull of the active session table (used for rate sampling)."""
    creds = _creds()
    if not creds:
        return []
    base, user, pw = creds
    sess = _Session(base)
    rows: list[dict] = []
    try:
        if sess.login(user, pw):
            data = sess.monitor_get("/api/v2/monitor/firewall/session?count=2000")
            res = data.get("results") or {}
            rows = res.get("details") if isinstance(res, dict) else res
            rows = [r for r in (rows or []) if isinstance(r, dict)]
    except Exception:
        rows = []
    finally:
        sess.logout()
    return rows


def _session_table() -> list[dict]:
    """Active firewall sessions, briefly cached — 6.2 has no per-source filter,
    so the gateway pulls one batch and filters locally."""
    if not _creds():
        return []
    with _flow_lock:
        if time.time() - _sess_cache["at"] < 20 and _sess_cache["rows"]:
            return _sess_cache["rows"]
    rows = _session_table_fresh()
    with _flow_lock:
        if rows:
            _sess_cache.update(at=time.time(), rows=rows)
        return _sess_cache["rows"]


def live_flows(ip: str) -> dict[str, Any] | None:
    """Current sessions + FortiView realtime destination aggregate for one host.

    Returns None when no credentials are configured (the profile then rests on
    the syslog evidence alone). FortiView resolves destination hostnames — the
    closest thing to "which sites does this device visit" the router can attest.
    """
    creds = _creds()
    if not creds:
        return None
    base, user, pw = creds

    dests: list[dict] = []
    sess = _Session(base)
    try:
        if sess.login(user, pw):
            q = urllib.parse.quote(json.dumps({"source": ip}))
            data = sess.monitor_get(
                f"/api/v2/monitor/fortiview/statistics?realtime=true&report_by=destination&count=12&filter={q}"
            )
            res = data.get("results") or {}
            for r in res.get("details", []) or []:
                if not isinstance(r, dict):
                    continue
                dests.append({
                    "ip": r.get("dstaddr"),
                    "resolved": r.get("resolved"),
                    "port": r.get("dst_port"),
                    "sessions": r.get("sessions"),
                    "country": r.get("country"),
                    "bytes": (r.get("sentbyte") or 0) + (r.get("rcvdbyte") or 0),
                })
    except Exception:
        pass
    finally:
        sess.logout()

    bw = live_bandwidth()
    # map dst IP -> the QoS shaper policy name the FortiGate applies (from the
    # session's own shaper field). This is a *policy label*, not necessarily a cap:
    # "guarantee-100kbps" is a floor, "400M" a ceiling — we can't read which from
    # the read-only monitor API, so we surface the name and never assert "limit".
    dst_shaper: dict[str, str] = {}
    for r in _session_table():
        if r.get("saddr") == ip and r.get("daddr") and r.get("shaper"):
            dst_shaper.setdefault(r["daddr"], r["shaper"])
    # roll current bandwidth up to each destination IP (sum across its ports)
    dst_bps: dict[str, int] = {}
    for (sa, da, _p), v in bw.items():
        if sa == ip and da:
            dst_bps[da] = dst_bps.get(da, 0) + (v["bps"] or 0)
    for d in dests:
        d["bps"] = dst_bps.get(d["ip"], 0)
        d["shaper"] = dst_shaper.get(d["ip"])

    conns = []
    for r in _session_table():
        sa, da = r.get("saddr"), r.get("daddr")
        if ip not in (sa, da):
            continue
        k = (sa, da, r.get("dport"))
        v = bw.get(k, {})
        conns.append({
            "dir": "out" if sa == ip else "in",
            "peer": da if sa == ip else sa,
            "port": r.get("dport"),
            "proto": r.get("proto"),
            "country": r.get("country"),
            "duration": r.get("duration"),
            "bps": v.get("bps", 0),
            "shaper": r.get("shaper"),
        })
    if not dests and not conns:
        return {"destinations": [], "sessions": [], "sessionCount": 0} if _creds() else None
    total_bps = sum(v["bps"] or 0 for (sa, _d, _p), v in bw.items() if sa == ip)
    return {
        "destinations": dests,
        "sessions": conns[:20],
        "sessionCount": len(conns),
        "totalBps": total_bps,
    }


def _ago_text(sec: int, lang: str) -> str:
    zh = lang == "zh"
    if sec < 90:
        return "刚刚" if zh else "just now"
    if sec < 3600:
        m = sec // 60
        return f"{m} 分钟前" if zh else f"{m} min ago"
    if sec < 86400:
        h = sec / 3600
        return f"{h:.1f} 小时前" if zh else f"{h:.1f} h ago"
    d = sec / 86400
    return f"{d:.1f} 天前" if zh else f"{d:.1f} days ago"


def device_status(ip: str, lang: str = "zh") -> dict[str, Any] | None:
    """Online / idle / offline verdict + 'last seen' for one host.

    Signals, in order: a live firewall session (definitely online now); else the
    device-inventory `last_seen` age (seconds since the FortiGate last detected it).
    Returns None when no credentials are configured.
    """
    if not _creds():
        return None
    has_session = any(
        ip in (r.get("saddr"), r.get("daddr")) for r in _session_table()
    )
    ids = identity_map()
    info = None
    for m in (ids["byIp"].get(ip),):
        if m:
            info = m
            break
    last = info.get("lastSeen") if info else None
    if has_session:
        state = "online"
    elif isinstance(last, int):
        state = "online" if last <= 120 else "idle" if last <= 1800 else "offline"
    else:
        state = "unknown"
    return {
        "state": state,
        "lastSeenSec": last if isinstance(last, int) else None,
        "lastSeenText": _ago_text(last, lang) if isinstance(last, int) else None,
        "hasLiveSession": has_session,
    }


def edge_bandwidth(ip_a: str, ip_b: str) -> dict[str, Any] | None:
    """Live bandwidth of the actual traffic between two hosts, either direction.

    For a relation edge in the constellation, most pairs never exchange traffic
    (the link is a correlation, not a flow) → returns {active: False}. When a real
    session exists, returns its current bps + configured shaper cap.
    """
    if not _creds():
        return None
    bw = live_bandwidth()
    bps = 0
    limit = None
    active = False
    for (sa, da, _p), v in bw.items():
        if {sa, da} == {ip_a, ip_b}:
            active = True
            bps += v["bps"] or 0
            if v.get("limitBps"):
                limit = max(limit or 0, v["limitBps"])
    return {"active": active, "bps": bps, "limitBps": limit}
