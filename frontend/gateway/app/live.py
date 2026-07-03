"""Live signals: real-time event rate from R230, and on-demand DeepSeek threat analysis."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

# ---- real-time event rate (poll R230 syslog tail) ----
_rate_cache: dict[str, Any] = {"at": 0.0, "val": None}
_RATE_TTL = 4.0


def event_rate() -> dict[str, Any]:
    now = time.monotonic()
    if _rate_cache["val"] is not None and now - _rate_cache["at"] <= _RATE_TTL:
        return _rate_cache["val"]
    val = _probe_rate()
    _rate_cache.update(at=now, val=val)
    return val


def _probe_rate() -> dict[str, Any]:
    ssh = os.getenv("R230_SSH")
    pw = os.getenv("R230_PASS")
    log = os.getenv("R230_LOG", "/data/fortigate-runtime/input/fortigate.log")
    if not ssh or not pw:
        return {"eventsPerSec": None, "lines": 0, "live": False}
    # tail recent lines, read first+last timestamp, derive lines/sec
    # match the HH:MM:SS value of " time=" (not "eventtime=" epoch); first+last of the tail
    pat = "[^a-zA-Z]time=([0-9]{1,2}:[0-9]{2}:[0-9]{2})"
    remote = f"tail -n 6000 {log} | sed -nE '1s/.*{pat}.*/\\1/p; $s/.*{pat}.*/\\1/p'"
    try:
        out = subprocess.run(
            ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=6", ssh, remote],
            capture_output=True, text=True, timeout=12,
        ).stdout.strip().splitlines()
    except Exception:
        return {"eventsPerSec": None, "lines": 0, "live": False}
    times = [t for t in out if re.match(r"^\d+:\d+:\d+$", t)]
    if len(times) < 2:
        return {"eventsPerSec": None, "lines": 0, "live": False}

    def secs(t: str) -> int:
        h, m, s = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + s

    span = secs(times[-1]) - secs(times[0])
    if span <= 0:
        span = 1
    rate = round(6000 / span, 1)
    return {"eventsPerSec": rate, "lines": 6000, "spanSec": span, "live": True}


# ---- on-demand DeepSeek threat analysis for one device ----
def assess_device(ip: str, cidr: str, device: dict, lang: str = "zh", peers: list | None = None) -> dict[str, Any]:
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}
    client = OpenAICompatibleClient(
        base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=45
    )
    want_lang = "Chinese" if lang == "zh" else "English"
    candidates = [
        {"ip": p["ip"], "ports": p.get("top_ports"), "deny": p.get("deny"), "threat": p.get("threat")}
        for p in (peers or [])
        if p.get("ip") != ip
    ][:8]
    instr = (
        f"You are a network threat analyst. Assess this internal host from real FortiGate "
        f"telemetry and predict its blast radius. Respond in {want_lang}, concrete and affirmative. "
        f"Pick impact_peers ONLY from candidate_hosts that share ports / behaviour with this host. "
        f"Return JSON {{"
        f'"verdict": <short label>, "severity": "high|medium|low", '
        f'"analysis": <2 sentences>, '
        f'"impact_peers": [{{"ip": <candidate ip>, "relation": <very short>}}], '
        f'"most_likely": <1 sentence outcome>, "worst_case": <1 sentence>, '
        f'"recovery": {{"action": <short>, "eta": <e.g. 2-4h>}}}}.'
    )
    payload = {
        "host": ip,
        "subnet": cidr,
        "denied_flows": device.get("deny"),
        "accepted_flows": device.get("accept"),
        "total_flows": device.get("flows"),
        "top_target_ports": device.get("top_ports"),
        "prior_threat_score": device.get("threat"),
        "candidate_hosts": candidates,
    }
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps(payload)}],
            schema_name="threat_assessment",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    rec = out.get("recovery") or {}
    valid = {c["ip"] for c in candidates}
    return {
        "ok": True,
        "ip": ip,
        "verdict": out.get("verdict") or out.get("label") or "",
        "severity": out.get("severity", device.get("threat", "")),
        "analysis": out.get("analysis") or out.get("text") or "",
        "impactPeers": [p for p in (out.get("impact_peers") or []) if p.get("ip") in valid][:6],
        "mostLikely": out.get("most_likely", ""),
        "worstCase": out.get("worst_case", ""),
        "recovery": {"action": rec.get("action", ""), "eta": rec.get("eta", "")},
        "model": cfg["model"],
    }


def assess_subnet(cidr: str, lang: str = "zh") -> dict[str, Any]:
    """Batch-research every flagged device in a subnet, then synthesize the posture."""
    from concurrent.futures import ThreadPoolExecutor

    from .rca_reader import _load_topology

    topo = _load_topology() or {}
    sub = next((s for s in topo.get("subnets", []) if s.get("cidr") == cidr), None)
    if sub is None:
        return {"ok": False, "text": "subnet not found"}
    targets = [d for d in (sub.get("devices") or []) if d.get("threat") in ("high", "watch")]
    if not targets:
        return {"ok": True, "cidr": cidr, "devices": [], "posture": {"high": 0, "watch": 0, "summary": ""}}

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        results = list(pool.map(lambda d: assess_device(d["ip"], cidr, d, lang), targets))
    results = [r for r in results if r.get("ok")]
    high = sum(1 for r in results if r.get("severity") == "high")
    watch = len(results) - high
    summary = _synthesize_posture(cidr, results, lang)
    return {"ok": True, "cidr": cidr, "devices": results, "posture": {"high": high, "watch": watch, "summary": summary}}


_mesh_cache: dict[str, Any] = {}


def assess_mesh(cidr: str, lang: str = "zh") -> dict[str, Any]:
    """DeepSeek models the whole subnet: enriched device profiles + relationship links + clusters."""
    from . import providers
    from .rca_reader import _load_meshes
    from core.llm.provider import OpenAICompatibleClient

    ck = f"{cidr}:{lang}"
    if ck in _mesh_cache:
        return _mesh_cache[ck]
    nodes_in = (_load_meshes() or {}).get(cidr, [])
    if not nodes_in:
        return {"ok": False, "text": "no mesh for subnet"}
    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}
    client = OpenAICompatibleClient(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=60)
    want = "Chinese" if lang == "zh" else "English"
    devs = [{"ip": n["ip"], "role": n["role"], "ports": n["ports"], "deny": n["deny"], "out": n["out"], "threat": n["threat"]} for n in nodes_in]
    instr = (
        f"You are a SOC analyst modeling subnet {cidr} from real FortiGate device profiles. "
        f"Respond in {want}. Build a relationship model. Use ONLY the given IPs. "
        f"Return JSON {{"
        f'"nodes": [{{"ip": <ip>, "label": <2-4 word role/function>, "severity": "high|medium|low", "summary": <<=10 words>}}], '
        f'"links": [{{"src": <ip>, "dst": <ip>, "relation": <<=6 words>, "strength": 1-3}}], '
        f'"clusters": [{{"name": <short>, "members": [<ip>], "note": <<=12 words>}}]}}. '
        f"Link devices that share scan-target ports or form a campaign; cluster by behaviour/role. "
        f"Keep links to the most meaningful <=24."
    )
    try:
        out = client.complete_json([{"role": "user", "content": instr + "\n" + json.dumps(devs)}], schema_name="mesh_model")
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    valid = {d["ip"] for d in devs}
    nodes = [n for n in (out.get("nodes") or []) if n.get("ip") in valid]
    links = [l for l in (out.get("links") or []) if l.get("src") in valid and l.get("dst") in valid and l.get("src") != l.get("dst")][:24]
    clusters = [
        {**c, "members": [m for m in (c.get("members") or []) if m in valid]}
        for c in (out.get("clusters") or [])
    ]
    # carry the raw metrics for sizing/colour
    raw = {n["ip"]: n for n in nodes_in}
    for n in nodes:
        r = raw.get(n["ip"], {})
        n["out"] = r.get("out", 0)
        n["deny"] = r.get("deny", 0)
        n["ports"] = r.get("ports", [])
        n["role"] = r.get("role", n.get("label", ""))
    result = {"ok": True, "cidr": cidr, "nodes": nodes, "links": links, "clusters": clusters, "model": cfg["model"]}
    _mesh_cache[ck] = result
    return result


def _wan_evidence() -> dict[str, Any]:
    """Full real WAN attack evidence from the held-out window stats, clustered by /24."""
    from collections import defaultdict
    from pathlib import Path

    from .rca_reader import _MANIFEST
    from domains.network_rca.real_dataset import resolve_stats_path

    s = json.loads(Path(resolve_stats_path(_MANIFEST)).read_text(encoding="utf-8"))
    top = s.get("admin_login_failed_top_src", [])
    blocks: dict[str, dict] = defaultdict(lambda: {"count": 0, "ips": []})
    for row in top:
        ip, n = row[0], row[1]
        net = ".".join(ip.split(".")[:3]) + ".0/24"
        blocks[net]["count"] += n
        blocks[net]["ips"].append([ip, n])
    netblocks = sorted(
        [{"cidr": k, "count": v["count"], "ips": v["ips"]} for k, v in blocks.items()],
        key=lambda b: -b["count"],
    )
    return {
        "adminLoginFailed": s.get("admin_login_failed", 0),
        "distinctSrc": s.get("admin_login_failed_distinct_src", 0),
        "lockouts": s.get("admin_login_disabled_lockouts", 0),
        "topAttackers": top,
        "netblocks": netblocks,
        "internalDenySrc": s.get("deny_top_src", []),
        "denyPorts": s.get("deny_top_dstports", []),
        "denyCount": s.get("deny_count", 0),
    }


_wan_cache: dict[str, Any] = {}


def assess_wan(ip: str, lang: str = "zh") -> dict[str, Any]:
    """DeepSeek deep-analysis of one WAN attacker: campaign correlation + cross-side blast radius.

    Grounded entirely in the real held-out FortiGate window: the coordinated brute-force
    campaign (netblock lockstep), the admin lockouts it caused, and whether the internal
    deny-heavy hosts represent post-compromise pivot activity.
    """
    ck = f"{ip}:{lang}"
    if ck in _wan_cache:
        return _wan_cache[ck]
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    ev = _wan_evidence()
    attacker = next((a for a in ev["topAttackers"] if a[0] == ip), None)
    if attacker is None:
        return {"ok": False, "text": "attacker not in held-out top sources"}
    net = ".".join(ip.split(".")[:3]) + ".0/24"
    block = next((b for b in ev["netblocks"] if b["cidr"] == net), None) or {"cidr": net, "count": attacker[1], "ips": [attacker]}
    siblings_ev = [[i, n] for i, n in block["ips"] if i != ip]
    internal = ev["internalDenySrc"][:6]

    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}
    client = OpenAICompatibleClient(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=50)
    want = "Chinese" if lang == "zh" else "English"
    instr = (
        f"You are a SOC threat analyst. Assess this external WAN source that is hammering the "
        f"FortiGate admin login, using ONLY the real evidence provided. Respond in {want}, concise "
        f"and affirmative. Judge whether this is part of a COORDINATED campaign (identical attempt "
        f"counts across sibling IPs in the same /24 = botnet lockstep). Then judge whether the "
        f"internal_deny_hosts are plausibly POST-COMPROMISE pivots (heavy internal scanning) tied to "
        f"this credential attack, and pick only the ones that fit. Return JSON {{"
        f'"verdict": <short label>, "severity": "critical|high|medium", '
        f'"campaign": <1 sentence: coordinated or isolated + the signal>, '
        f'"kill_chain": <one of: recon|credential-access|lateral-movement|impact>, '
        f'"attribution": <short: botnet/netblock signal>, '
        f'"siblings": [{{"ip": <sibling ip from same_netblock>, "note": <<=6 words>}}], '
        f'"internal_correlation": [{{"ip": <ip from internal_deny_hosts>, "relation": <why linked, <=8 words>}}], '
        f'"blast": <1 sentence: lockout DoS / compromise risk>, '
        f'"actions": [<concrete action>, <concrete action>, <concrete action>], '
        f'"confidence": <0-1 number>}}.'
    )
    payload = {
        "wan_source": ip,
        "attempts": attacker[1],
        "same_netblock": {"cidr": block["cidr"], "total_attempts": block["count"], "sibling_ips": siblings_ev},
        "campaign_totals": {
            "distinct_sources": ev["distinctSrc"],
            "total_admin_failures": ev["adminLoginFailed"],
            "admin_lockouts_triggered": ev["lockouts"],
        },
        "internal_deny_hosts": [{"ip": i, "denied_flows": n} for i, n in internal],
        "top_denied_ports": ev["denyPorts"][:6],
    }
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps(payload)}],
            schema_name="wan_threat",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}

    sib_valid = {i for i, _ in siblings_ev}
    int_valid = {i for i, _ in internal}
    return {
        "ok": True,
        "ip": ip,
        "attempts": attacker[1],
        "netblock": block["cidr"],
        "netblockAttempts": block["count"],
        "verdict": out.get("verdict", ""),
        "severity": out.get("severity", "high"),
        "campaign": out.get("campaign", ""),
        "killChain": out.get("kill_chain", ""),
        "attribution": out.get("attribution", ""),
        "siblings": [
            {"ip": x.get("ip"), "note": x.get("note", ""), "attempts": dict(siblings_ev).get(x.get("ip"))}
            for x in (out.get("siblings") or []) if x.get("ip") in sib_valid
        ][:6],
        "internalCorrelation": [
            {"ip": x.get("ip"), "relation": x.get("relation", ""), "deny": dict(internal).get(x.get("ip"))}
            for x in (out.get("internal_correlation") or []) if x.get("ip") in int_valid
        ][:6],
        "blast": out.get("blast", ""),
        "actions": [a for a in (out.get("actions") or []) if a][:4],
        "confidence": out.get("confidence"),
        "lockouts": ev["lockouts"],
        "distinctSrc": ev["distinctSrc"],
        "model": cfg["model"],
    }


def _synthesize_posture(cidr: str, results: list[dict], lang: str) -> str:
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    cfg = providers._deepseek_cfg()
    if not cfg["api_key"] or not results:
        return ""
    client = OpenAICompatibleClient(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=40)
    want = "Chinese" if lang == "zh" else "English"
    brief = [{"ip": r["ip"], "severity": r["severity"], "verdict": r["verdict"]} for r in results]
    instr = (
        f"You are a SOC lead. Given per-host findings on subnet {cidr}, write a 2-sentence "
        f"situational summary in {want}: overall posture and the single highest-priority action. "
        f'Return JSON {{"summary": <text>}}.'
    )
    try:
        out = client.complete_json([{"role": "user", "content": instr + "\n" + json.dumps(brief)}], schema_name="posture")
        return out.get("summary", "")
    except Exception:
        return ""
