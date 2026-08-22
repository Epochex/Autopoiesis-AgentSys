"""Per-device traffic portrait mined from the raw FortiGate syslog.

Answers, for one host: where its traffic GOES (destinations, services, bytes),
what COMES AT it (inbound sources), when it is active, and what those flows say
about the device's role. FortiGate traffic lines carry no hostnames, so external
endpoints are reported as ip + service + country, with best-effort reverse DNS
filling in a readable name when the resolver answers quickly.

Aggregates are computed over the committed syslog sample (see manifest notes:
the full capture lives on R230), parsed once and cached on file mtimes.
"""
from __future__ import annotations

import json
import re
import socket
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path
from core.net.addr import is_multicast_or_broadcast, is_private
from typing import Any

from .rca_reader import _REPO_ROOT, _load_device_graphs

_REAL = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real"

_FIELD = re.compile(
    r'srcip=(?P<src>[0-9.]+).*?dstip=(?P<dst>[0-9.]+)'
    r'(?:.*?dstport=(?P<port>\d+))?'
    r'(?:.*?proto=(?P<proto>\d+))?'
    r'(?:.*?action="(?P<action>[a-z]+)")?'
    r'(?:.*?service="(?P<service>[^"]+)")?'
    r'(?:.*?dstcountry="(?P<country>[^"]+)")?'
    r'(?:.*?sentbyte=(?P<sent>\d+))?'
    r'(?:.*?rcvdbyte=(?P<rcvd>\d+))?'
)
_VOIP = re.compile(r'srcip=(?P<src>[0-9.]+) src_port=\d+ dstip=(?P<dst>[0-9.]+) dst_port=(?P<port>\d+)')
_TIME = re.compile(r' time=(?P<h>\d{2}):\d{2}:\d{2}')
_DATE = re.compile(r'date=(?P<d>\d{4}-\d{2}-\d{2})')

# IP protocol numbers as syslog reports them (proto=6 etc.)
_PROTO = {1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 47: "GRE", 50: "ESP", 58: "ICMPv6"}

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_rdns_cache: dict[str, str | None] = {}


def _is_private(ip: str) -> bool:
    """See core.net.addr; kept as a local name for the many call sites."""
    return is_private(ip)


def _is_bcast(ip: str) -> bool:
    """A /24 broadcast address, or any group address; see core.net.addr."""
    return ip.endswith(".255") or is_multicast_or_broadcast(ip)


def _log_paths() -> list[Path]:
    try:
        manifest = json.loads((_REAL / "manifest.json").read_text(encoding="utf-8"))
        return [p for p in (_REAL / rel for rel in manifest.get("syslog_paths", [])) if p.exists()]
    except Exception:
        return []


def _mine() -> dict[str, Any]:
    """Parse every traffic/voip line once into per-pair aggregates; mtime-cached."""
    paths = _log_paths()
    key = tuple((str(p), p.stat().st_mtime) for p in paths)
    with _lock:
        if _cache.get("key") == key:
            return _cache["data"]
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    hours: dict[str, list[int]] = {}
    span: dict[str, list[str]] = {}
    # per-host (side, port, proto) usage: side "local" = a port ON the host that
    # peers hit; side "remote" = a destination port the host reaches out to.
    port_use: dict[str, dict[tuple[str, int, str], dict[str, Any]]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            is_traffic = 'type="traffic"' in line
            is_voip = 'subtype="voip"' in line
            if not (is_traffic or is_voip):
                continue
            m = (_FIELD if is_traffic else _VOIP).search(line)
            if not m:
                continue
            g = m.groupdict()
            src, dst = g["src"], g["dst"]
            p = pairs.setdefault((src, dst), {
                "hits": 0, "accept": 0, "deny": 0, "sent": 0, "rcvd": 0,
                "ports": Counter(), "services": Counter(), "country": None,
            })
            p["hits"] += 1
            action = g.get("action") or ("accept" if is_voip else None)
            if action == "deny":
                p["deny"] += 1
            elif action:
                p["accept"] += 1
            if g.get("port"):
                p["ports"][int(g["port"])] += 1
            if g.get("service"):
                p["services"][g["service"]] += 1
            if is_voip:
                p["services"]["SIP"] += 1
            if g.get("country") and g["country"] not in ("Reserved",):
                p["country"] = g["country"]
            p["sent"] += int(g.get("sent") or 0)
            p["rcvd"] += int(g.get("rcvd") or 0)
            if g.get("port"):
                port = int(g["port"])
                proto = (_PROTO.get(int(g["proto"]), f"IP/{g['proto']}") if g.get("proto")
                         else ("UDP" if is_voip else "?"))
                svc = g.get("service") or ("SIP" if is_voip else None)
                for host, side in ((src, "remote"), (dst, "local")):
                    if not _is_private(host) or _is_bcast(host):
                        continue
                    u = port_use.setdefault(host, {}).setdefault(
                        (side, port, proto), {"hits": 0, "deny": 0, "services": Counter()})
                    u["hits"] += 1
                    if action == "deny":
                        u["deny"] += 1
                    if svc:
                        u["services"][svc] += 1
            tm = _TIME.search(line)
            dm = _DATE.search(line)
            for ip in (src, dst):
                if not _is_private(ip):
                    continue
                if tm:
                    hours.setdefault(ip, [0] * 24)[int(tm.group("h"))] += 1
                if dm:
                    s = span.setdefault(ip, [dm.group("d"), dm.group("d")])
                    s[0] = min(s[0], dm.group("d"))
                    s[1] = max(s[1], dm.group("d"))
    data = {"pairs": pairs, "hours": hours, "span": span, "port_use": port_use}
    with _lock:
        _cache["key"] = key
        _cache["data"] = data
    return data


def _rdns(ips: list[str]) -> dict[str, str | None]:
    """Best-effort reverse DNS for external endpoints; never blocks the response."""
    todo = [ip for ip in ips if ip not in _rdns_cache]
    if todo:
        # shutdown(wait=False): a slow resolver must not hold the response hostage —
        # unresolved names stay None and get retried on the next request.
        ex = ThreadPoolExecutor(max_workers=6)
        futs = {ip: ex.submit(lambda i: socket.gethostbyaddr(i)[0], ip) for ip in todo}
        for ip, fut in futs.items():
            try:
                _rdns_cache[ip] = fut.result(timeout=1.5)
            except FutTimeout:
                pass  # not cached → retried on a later request
            except Exception:
                _rdns_cache[ip] = None  # NXDOMAIN etc. — a real answer, cache it
        ex.shutdown(wait=False, cancel_futures=True)
    return {ip: _rdns_cache.get(ip) for ip in ips}


def _peer_row(peer: str, agg: dict[str, Any], *, inbound: bool) -> dict[str, Any]:
    return {
        "ip": peer,
        "hits": agg["hits"],
        "accept": agg["accept"],
        "deny": agg["deny"],
        "bytes": agg["sent"] + agg["rcvd"],
        "ports": [p for p, _ in agg["ports"].most_common(4)],
        "services": [s for s, _ in agg["services"].most_common(3)],
        "country": agg["country"],
        "external": not _is_private(peer) and not _is_bcast(peer),
        "kind": "bcast" if _is_bcast(peer) else "host",
        "dir": "in" if inbound else "out",
    }


def _tags(dev: dict | None, out: list[dict], inn: list[dict], lang: str) -> list[str]:
    zh = lang == "zh"
    t: list[str] = []
    out_hits = sum(r["hits"] for r in out)
    in_hits = sum(r["hits"] for r in inn)
    ext = [r for r in out if r["external"]]
    bcast = sum(r["hits"] for r in out if r["kind"] == "bcast")
    deny = sum(r["deny"] for r in out + inn)
    total = out_hits + in_hits
    if not total:
        t.append("采样窗内无流量 · 仅 DHCP 可见" if zh else "no flows in sample window · DHCP-only")
        return t
    if ext:
        lands = sorted({r["country"] for r in ext if r["country"]})
        t.append((f"有外联 · {len(ext)} 个外部端点" if zh else f"talks to WAN · {len(ext)} external peers")
                 + (f" · {'/'.join(lands[:3])}" if lands else ""))
    else:
        t.append("纯内网通信" if zh else "internal-only")
    if in_hits > out_hits * 3 and in_hits > 20:
        t.append("服务端特征 · 以被动连接为主" if zh else "server-like · mostly inbound")
    elif out_hits > in_hits * 3 and out_hits > 20:
        t.append("客户端特征 · 以主动外发为主" if zh else "client-like · mostly outbound")
    if out:
        top = out[0]
        if top["hits"] > 100 and len(top["ports"]) == 1:
            t.append((f"固定端口高频心跳 · :{top['ports'][0]}" if zh else f"fixed-port heartbeat · :{top['ports'][0]}"))
    if bcast > total * 0.5 and bcast > 30:
        t.append("广播喧哗 · 发现/组播为主" if zh else "broadcast-noisy · discovery traffic")
    if deny > total * 0.5 and deny > 20:
        t.append("大量流量被策略拦截" if zh else "heavily policy-denied")
    if dev and dev.get("role") and dev["role"] != "unknown":
        role_zh = {"camera": "摄像头", "intercom": "门禁对讲", "mobile": "移动端", "workstation": "工作站", "server": "服务器"}
        t.append((f"指纹角色: {role_zh.get(dev['role'], dev['role'])}" if zh else f"fingerprint role: {dev['role']}"))
    return t[:6]


def device_profile(ip: str, lang: str = "zh") -> dict[str, Any]:
    """Traffic portrait for one host: destinations, inbound sources, activity, inferred tags."""
    data = _mine()
    dev = None
    for sub in _load_device_graphs().values():
        for d in sub.get("devices", []):
            if isinstance(d, dict) and d.get("ip") == ip:
                dev = d
                break
        if dev:
            break
    if dev is not None:
        from .live_identity import enrich_device
        dev = enrich_device(dev)

    out_rows = sorted(
        (_peer_row(dst, agg, inbound=False) for (src, dst), agg in data["pairs"].items() if src == ip),
        key=lambda r: -r["hits"],
    )
    in_rows = sorted(
        (_peer_row(src, agg, inbound=True) for (src, dst), agg in data["pairs"].items() if dst == ip),
        key=lambda r: -r["hits"],
    )
    names = _rdns([r["ip"] for r in (out_rows + in_rows) if r["external"]][:12])
    for r in out_rows + in_rows:
        r["rdns"] = names.get(r["ip"])

    sp = data["span"].get(ip)
    try:
        from .live_identity import device_status, live_flows
        live = live_flows(ip)
        status = device_status(ip, lang)
    except Exception:
        live = None
        status = None

    tags = _tags(dev, out_rows, in_rows, lang)
    # the sampled syslog can be blind to a host the router sees active right now;
    # let the live view correct a misleading "no traffic" verdict.
    if live and live.get("destinations"):
        zh = lang == "zh"
        ext = [d for d in live["destinations"] if d.get("resolved") or d.get("country")]
        lead = (f"实时活跃 · {live['sessionCount']} 会话 · {len(live['destinations'])} 个目标"
                if zh else f"live-active · {live['sessionCount']} sessions · {len(live['destinations'])} dests")
        tags = [lead] + [t for t in tags if "无流量" not in t and "no flows" not in t]
        sites = [d["resolved"] for d in ext if d.get("resolved")][:3]
        if sites:
            tags.append(("常访问: " if zh else "visits: ") + ", ".join(sites))
        tags = tags[:7]

    def _port_rows(side: str) -> list[dict[str, Any]]:
        rows = []
        for (s, port, proto), u in (data["port_use"].get(ip) or {}).items():
            if s != side:
                continue
            # drop service labels that just restate port/proto ("udp/48689")
            svc = next((name for name, _ in u["services"].most_common(2)
                        if name.lower() != f"{proto.lower()}/{port}"), None)
            rows.append({"port": port, "proto": proto, "service": svc,
                         "hits": u["hits"], "deny": u["deny"]})
        rows.sort(key=lambda r: -r["hits"])
        return rows

    return {
        "ok": True,
        "ip": ip,
        "device": dev,
        "status": status,
        "live": live,
        "ports": {"local": _port_rows("local"), "remote": _port_rows("remote")},
        "outbound": out_rows[:10],
        "inbound": in_rows[:8],
        "hours": data["hours"].get(ip, [0] * 24),
        "tags": tags,
        "totals": {
            "outHits": sum(r["hits"] for r in out_rows),
            "inHits": sum(r["hits"] for r in in_rows),
            "bytes": sum(r["bytes"] for r in out_rows + in_rows),
            "extPeers": sum(1 for r in out_rows + in_rows if r["external"]),
            "intPeers": sum(1 for r in out_rows + in_rows if not r["external"] and r["kind"] == "host"),
        },
        "window": {"from": sp[0], "to": sp[1]} if sp else None,
        "sampled": True,
    }
