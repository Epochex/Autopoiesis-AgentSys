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
