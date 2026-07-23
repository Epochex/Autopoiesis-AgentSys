"""Live signals: real-time event rate from R230, and on-demand DeepSeek threat analysis."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

from .evidence_gate import (
    evidence_fact,
    relationship_evidence,
    verify_graph_claims,
    verify_pair_claims,
)

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
        {"ip": p["ip"], "ports": p.get("top_ports") or p.get("topPorts"), "deny": p.get("deny"), "threat": p.get("threat"), "role": p.get("role")}
        for p in (peers or [])
        if p.get("ip") != ip
    ][:8]
    source_profile = {
        "ip": ip,
        "top_ports": device.get("top_ports") or device.get("topPorts") or [],
        "deny": device.get("deny"),
        "threat": device.get("threat"),
        "role": device.get("role"),
    }
    relation_evidence = [
        fact
        for candidate in candidates
        if (fact := relationship_evidence(source_profile, candidate)) is not None
    ]
    supported_candidates = {
        subject
        for fact in relation_evidence
        for subject in fact["subjects"]
        if subject != ip
    }
    candidates = [
        {
            **candidate,
            "evidence_ids": [
                fact["evidence_id"]
                for fact in relation_evidence
                if candidate["ip"] in fact["subjects"]
            ],
        }
        for candidate in candidates
        if candidate["ip"] in supported_candidates
    ]
    instr = (
        f"You are a network threat analyst. Assess this internal host from real FortiGate "
        f"telemetry and predict its blast radius. Respond in {want_lang}, concrete and affirmative. "
        f"Pick impact_peers ONLY from candidate_hosts that share ports / behaviour with this host. "
        f"Return JSON {{"
        f'"verdict": <short label>, "severity": "high|medium|low", '
        f'"analysis": <2 sentences>, '
        f'"impact_peers": [{{"ip": <candidate ip>, "relation": <very short>, '
        f'"evidence_ids": [<ids from candidate_hosts that support this exact host pair>]}}], '
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
        "relationship_evidence": relation_evidence,
    }
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps(payload)}],
            schema_name="threat_assessment",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    rec = out.get("recovery") or {}
    peer_claims = [p for p in (out.get("impact_peers") or []) if isinstance(p, dict)]
    impact_peers, rejected, errors = verify_pair_claims(
        peer_claims,
        relation_evidence,
        source_field="",
        target_field="ip",
        fixed_source=ip,
        field="impactPeers",
    )
    return {
        "ok": True,
        "ip": ip,
        "verdict": out.get("verdict") or out.get("label") or "",
        "severity": out.get("severity", device.get("threat", "")),
        "analysis": out.get("analysis") or out.get("text") or "",
        "impactPeers": impact_peers[:6],
        "mostLikely": out.get("most_likely", ""),
        "worstCase": out.get("worst_case", ""),
        "recovery": {"action": rec.get("action", ""), "eta": rec.get("eta", "")},
        "evidence": relation_evidence,
        "unverified": {"impactPeers": rejected},
        "verificationErrors": errors,
        "verificationStatus": "verified" if not errors else "partial",
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
    relation_evidence = [
        fact
        for index, source in enumerate(devs)
        for target in devs[index + 1 :]
        if (fact := relationship_evidence(source, target)) is not None
    ]
    evidence_by_pair = {tuple(fact["pair"]): fact["evidence_id"] for fact in relation_evidence}
    profiles = [
        {
            **device,
            "relationship_evidence_ids": [
                evidence_id for pair, evidence_id in evidence_by_pair.items() if device["ip"] in pair
            ],
        }
        for device in devs
    ]
    instr = (
        f"You are a SOC analyst modeling subnet {cidr} from real FortiGate device profiles. "
        f"Respond in {want}. Build a relationship model. Use ONLY the given IPs. "
        f"Return JSON {{"
        f'"nodes": [{{"ip": <ip>, "label": <2-4 word role/function>, "severity": "high|medium|low", "summary": <<=10 words>}}], '
        f'"links": [{{"src": <ip>, "dst": <ip>, "relation": <<=6 words>, "strength": 1-3, '
        f'"evidence_ids": [<ids that support this exact pair>]}}], '
        f'"clusters": [{{"name": <short>, "members": [<ip>], "note": <<=12 words>}}]}}. '
        f"Link devices that share scan-target ports or form a campaign; cluster by behaviour/role. "
        f"Keep links to the most meaningful <=24."
    )
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps({"profiles": profiles, "relationship_evidence": relation_evidence})}],
            schema_name="mesh_model",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    valid = {d["ip"] for d in devs}
    nodes = [n for n in (out.get("nodes") or []) if n.get("ip") in valid]
    link_claims = [link for link in (out.get("links") or []) if isinstance(link, dict)]
    links, rejected_links, errors = verify_pair_claims(
        link_claims,
        relation_evidence,
        source_field="src",
        target_field="dst",
        field="links",
    )
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
    result = {
        "ok": True,
        "cidr": cidr,
        "nodes": nodes,
        "links": links[:24],
        "clusters": clusters,
        "evidence": relation_evidence,
        "unverified": {"links": rejected_links},
        "verificationErrors": errors,
        "verificationStatus": "verified" if not errors else "partial",
        "model": cfg["model"],
    }
    _mesh_cache[ck] = result
    return result


_graph_cache: dict[str, Any] = {}


def subnet_graph(cidr: str) -> dict[str, Any]:
    """The raw mined device graph for one subnet: every host, every justified relation."""
    from .rca_reader import _load_device_graphs

    g = (_load_device_graphs() or {}).get(cidr)
    if not g:
        return {"ok": False, "text": "no device graph for subnet"}
    return {"ok": True, **g}


def analyze_graph(cidr: str, lang: str = "zh") -> dict[str, Any]:
    """The agent reads the whole segment: names the communities, then hunts for the
    patterns nobody asked about — shadow IoT fleets, netmask leaks, duplicate IPs,
    lateral-movement corridors — grounded ONLY in the mined evidence."""
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    ck = f"graph:{cidr}:{lang}"
    if ck in _graph_cache:
        return _graph_cache[ck]
    g = subnet_graph(cidr)
    if not g.get("ok"):
        return g
    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}

    devs = {d["ip"]: d for d in g["devices"]}
    deg: dict[str, int] = {}
    for e in g["edges"]:
        deg[e["src"]] = deg.get(e["src"], 0) + 1
        deg[e["dst"]] = deg.get(e["dst"], 0) + 1
    hubs = sorted(
        g["devices"],
        key=lambda d: (-(deg.get(d["ip"], 0)), -d["deny"], -d["flows"]),
    )[:14]
    strongest_edges = sorted(g["edges"], key=lambda edge: -edge["weight"])[:18]
    graph_edge_evidence = [
        evidence_fact(
            "graph_edge",
            {
                "src": edge["src"],
                "dst": edge["dst"],
                "kind": edge["kind"],
                "evidence": edge["evidence"],
                "observed": edge["observed"],
            },
            subjects=[edge["src"], edge["dst"]],
            pair=(edge["src"], edge["dst"]),
        )
        for edge in strongest_edges
    ]
    anomaly_evidence = [
        evidence_fact(
            "graph_anomaly",
            anomaly,
            subjects=[member for member in anomaly.get("members", []) if member in devs],
        )
        for anomaly in g["anomalies"]
    ]
    community_evidence = [
        evidence_fact(
            "graph_community",
            {
                "id": community["id"],
                "role": community["role"],
                "vendor": community["vendor"],
                "bound_by": community["boundBy"],
                "members": community["members"],
            },
            subjects=[member for member in community["members"] if member in devs],
        )
        for community in g["clusters"][:12]
    ]
    hub_evidence = [
        evidence_fact(
            "device_profile",
            {
                "ip": device["ip"],
                "links": deg.get(device["ip"], 0),
                "deny": device["deny"],
                "accept": device["accept"],
                "ports": device["topPorts"],
                "seen_by": device["seenBy"],
            },
            subjects=[device["ip"]],
        )
        for device in hubs
    ]
    graph_evidence = graph_edge_evidence + anomaly_evidence + community_evidence + hub_evidence
    anomaly_ids = {
        json.dumps(item["fact"], ensure_ascii=False, sort_keys=True): item["evidence_id"]
        for item in anomaly_evidence
    }
    payload = {
        "subnet": cidr,
        "stats": g["stats"],
        "communities": [
            {
                "id": c["id"], "size": c["size"], "role": c["role"], "vendor": c["vendor"],
                "bound_by": c["boundBy"], "denied_flows": c["deny"],
                "members": c["members"][:10],
                "sample_names": [devs[m]["name"] for m in c["members"][:6] if devs.get(m, {}).get("name")],
            }
            for c in g["clusters"][:12]
        ],
        "anomalies": [
            {
                **anomaly,
                "evidence_id": anomaly_ids[json.dumps(anomaly, ensure_ascii=False, sort_keys=True)],
            }
            for anomaly in g["anomalies"]
        ],
        "hub_devices": [
            {
                "ip": d["ip"], "name": d["name"], "vendor": d["vendor"], "role": d["role"],
                "links": deg.get(d["ip"], 0), "deny": d["deny"], "accept": d["accept"],
                "ports": d["topPorts"], "seen_by": d["seenBy"],
            }
            for d in hubs
        ],
        "strongest_relations": [
            {
                "a": edge["src"],
                "b": edge["dst"],
                "kind": edge["kind"],
                "evidence": edge["evidence"],
                "observed": edge["observed"],
                "evidence_id": fact["evidence_id"],
            }
            for edge, fact in zip(strongest_edges, graph_edge_evidence)
        ],
        "evidence_registry": graph_evidence,
        "evidence_key": {
            "clash": "same L3 tuple claimed by two hosts (duplicate IP / NAT reuse) — OBSERVED",
            "bcast": "both hosts broadcast to the same discovery target — OBSERVED",
            "codst": "both hosts talk to the same destination — OBSERVED",
            "fleet": "same MAC OUI vendor block — INFERRED",
            "family": "same DHCP hostname family — INFERRED",
            "lease": "DHCP leases renew in lockstep (shared switch/power domain) — INFERRED",
            "portfp": "identical destination-port fingerprint — INFERRED",
        },
    }
    want = "Chinese" if lang == "zh" else "English"
    instr = (
        f"You are a network forensics analyst reading a reconstructed device graph for segment {cidr}. "
        f"The FortiGate only logs L3, so intra-segment links were RECONSTRUCTED from DHCP leases, MAC OUI, "
        f"hostnames, broadcast targets, session clashes and shared destinations. Respond in {want}. "
        f"Use ONLY the IPs given. Be concrete and specific — no generic advice.\n"
        f"Do three things: (1) give each community a real functional name; (2) find the HIDDEN patterns a "
        f"human would miss — shadow IoT fleets nobody inventoried, netmask/segmentation errors, duplicate-IP "
        f"conflicts, hosts that bridge two communities (lateral-movement corridors), silent DHCP-only devices "
        f"that never route; (3) describe where the segment's traffic actually converges. "
        f'Return JSON {{'
        f'"summary": <2 sentences on this segment\'s real structure>, '
        f'"communities": [{{"id": <community id>, "label": <2-5 word functional name>, "note": <<=12 words>}}], '
        f'"patterns": [{{"title": <short>, "kind": "shadow-fleet|misconfig|duplicate-ip|lateral-corridor|blind-spot|exposure", '
        f'"members": [<ip>], "why": <1 sentence citing the evidence kind>, "severity": "high|medium|low", '
        f'"confidence": <0-1>, "evidence_ids": [<ids covering every member>]}}], '
        f'"corridors": [{{"src": <ip>, "dst": <ip>, "path": [<ordered IPs from src to dst>], '
        f'"why": <<=8 words: why this is a pivot path>, "evidence_ids": [<one graph_edge id per path edge>]}}], '
        f'"flow": <1 sentence: where traffic converges / what the capillary pattern is>, '
        f'"blind_spot": <1 sentence: what this graph still cannot see>, '
        f'"actions": [<concrete action>, <concrete action>, <concrete action>]}}. '
        f"Give 3-6 patterns, ranked by severity."
    )
    client = OpenAICompatibleClient(
        base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=90
    )
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps(payload, ensure_ascii=False)}],
            schema_name="device_graph_analysis",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}

    valid = set(devs)
    ids = {c["id"] for c in g["clusters"]}
    pattern_claims = [p for p in (out.get("patterns") or []) if isinstance(p, dict) and p.get("title")]
    corridor_claims = [c for c in (out.get("corridors") or []) if isinstance(c, dict)]
    patterns, corridors, rejected, errors = verify_graph_claims(
        pattern_claims,
        corridor_claims,
        graph_evidence,
        valid,
    )
    result = {
        "ok": True,
        "cidr": cidr,
        "summary": out.get("summary", ""),
        "communities": [
            {"id": c.get("id"), "label": c.get("label", ""), "note": c.get("note", "")}
            for c in (out.get("communities") or []) if c.get("id") in ids
        ],
        "patterns": patterns[:6],
        "corridors": corridors[:8],
        "flow": out.get("flow", ""),
        "blindSpot": out.get("blind_spot", ""),
        "actions": [a for a in (out.get("actions") or []) if a][:4],
        "evidence": graph_evidence,
        "unverified": rejected,
        "verificationErrors": errors,
        "verificationStatus": "verified" if not errors else "partial",
        "model": cfg["model"],
    }
    _graph_cache[ck] = result
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
        "source": "DAHUA_FORTIGATE (FG100E) · R230 192.168.1.23",
        "windowDays": s.get("window_days", []),
        "adminLoginFailed": s.get("admin_login_failed", 0),
        "distinctSrc": s.get("admin_login_failed_distinct_src", 0),
        "lockouts": s.get("admin_login_disabled_lockouts", 0),
        "topAttackers": top,
        "netblocks": netblocks,
        "internalDenySrc": s.get("deny_top_src", []),
        "denyPorts": s.get("deny_top_dstports", []),
        "denyCount": s.get("deny_count", 0),
        "acceptPermit": s.get("accept_permit_count", 0),
        # Dahua device service-port probing (37777/37809/…): the vendor-specific
        # scan surface, distinct from the generic deny ports above.
        "devicePortTop": s.get("device_service_port_top", []),
        "devicePortDeny": s.get("device_service_port_deny", 0),
        "sessionClash": s.get("session_clash", 0),
    }


def _asset_exposure() -> dict[str, Any]:
    """Internal exposure face from the real device graph, for the drill-down's asset
    dimension: per-subnet role/vendor mix + the actually-exposed devices (threat, open
    ports, or accepted traffic), so the panel can pivot an external attacker IP to the
    same IP's internal device profile."""
    from .rca_reader import _load_device_graphs

    graphs = _load_device_graphs()
    _FIELDS = ("ip", "mac", "vendor", "os", "role", "flows", "deny", "accept", "topPorts", "threat")
    subnets = []
    for cidr, sub in graphs.items():
        stats = sub.get("stats", {})
        devs = sub.get("devices", []) if isinstance(sub.get("devices"), list) else []
        exposed = [
            {k: d.get(k) for k in _FIELDS}
            for d in devs
            if isinstance(d, dict)
            and (d.get("threat") not in (None, "ok") or d.get("topPorts") or (d.get("accept") or 0) > 0)
        ]
        exposed.sort(key=lambda d: -((d.get("deny") or 0) + (d.get("flows") or 0)))
        subnets.append({
            "cidr": cidr,
            "devices": stats.get("devices", len(devs)),
            "withTraffic": stats.get("withTraffic", 0),
            "deny": stats.get("deny", 0),
            "roles": stats.get("roles", {}),
            "vendors": stats.get("vendors", {}),
            "exposed": exposed[:24],
        })
    subnets.sort(key=lambda x: -(x.get("deny") or 0))
    totals = {
        "devices": sum(s["devices"] for s in subnets),
        "exposed": sum(len(s["exposed"]) for s in subnets),
        "high": sum(1 for s in subnets for d in s["exposed"] if d.get("threat") == "high"),
        "cameras": sum(v for s in subnets for r, v in s["roles"].items() if r == "camera"),
    }
    return {"subnets": subnets, "totals": totals}


def wan_attack_surface() -> dict[str, Any]:
    """Public entry for the attack-surface drill-down: WAN offense + internal exposure."""
    ev = _wan_evidence()
    ev["assetExposure"] = _asset_exposure()
    return ev


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
    lateral = next((x for x in ev["internalDenySrc"] if x[0] == ip), None)
    if attacker is None and lateral is not None:
        # an INTERNAL host clicked from the lateral-source cluster: assess it as a
        # possible compromised pivot — the attack it received, its weakness, and the
        # remediation playbook for its device role — NOT as an external WAN attacker.
        return _assess_internal_host(ip, lateral, ev, lang)
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
        f'"playbook": [{{"target": <node label, e.g. "FortiGate 192.168.1.1" or an internal host ip>, '
        f'"targetIp": <ip or cidr this step acts on>, "layer": "firewall|host|segment", '
        f'"commands": [<REAL runnable command or config line — FortiOS CLI for the FortiGate, '
        f'shell/iptables for a host — concrete and correct; this is for a human operator to REVIEW, '
        f'it will NOT be auto-executed>], "why": <<=10 words>}}], '
        f'"impact_nodes": [<every ip or cidr on the attack path: the attacker /24, 192.168.1.1 the '
        f'FortiGate, and each internal pivot ip>], '
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
        # runnable remediation playbook, mapped to real target nodes. Display-only:
        # the console never executes any of it — the approval boundary is absolute.
        "playbook": [
            {
                "target": p.get("target", ""),
                "targetIp": p.get("targetIp", ""),
                "layer": p.get("layer", ""),
                "commands": [c for c in (p.get("commands") or []) if c][:6],
                "why": p.get("why", ""),
            }
            for p in (out.get("playbook") or [])
            if isinstance(p, dict) and p.get("commands")
        ][:6],
        "impactNodes": [x for x in (out.get("impact_nodes") or []) if x][:12],
        "confidence": out.get("confidence"),
        "lockouts": ev["lockouts"],
        "distinctSrc": ev["distinctSrc"],
        "model": cfg["model"],
    }


def _assess_internal_host(ip: str, lateral, ev: dict[str, Any], lang: str) -> dict[str, Any]:
    """Assess an internal host (clicked from the lateral-source cluster) as a possible
    compromised pivot: the attack it received, its weakness, and a role playbook."""
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}
    dev = None
    for s in _asset_exposure()["subnets"]:
        for d in s["exposed"]:
            if d["ip"] == ip:
                dev = {**d, "cidr": s["cidr"]}
                break
        if dev:
            break
    want = "Chinese" if lang == "zh" else "English"
    client = OpenAICompatibleClient(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=50)
    instr = (
        f"You are a SOC analyst. This is an INTERNAL host on the R230 network with heavy denied traffic. "
        f"Assess whether it is a COMPROMISED PIVOT tied to the external brute-force campaign, using ONLY "
        f"the real evidence. Respond in {want}, concise. Give the attack it plausibly received, its weakness "
        f"(open ports/role), and a remediation PLAYBOOK for its device role — REAL runnable commands for a "
        f"human to REVIEW, NOT auto-executed. Return JSON {{\"verdict\": <short>, \"severity\": "
        f"\"critical|high|medium\", \"campaign\": <1 sentence: attack received>, \"kill_chain\": "
        f"\"lateral-movement|impact|credential-access\", \"attribution\": <device role>, \"blast\": "
        f"<1 sentence: weakness>, \"actions\": [<action>,<action>], \"playbook\": [{{\"target\": <this host "
        f"or its gateway>, \"targetIp\": <ip>, \"layer\": \"host|firewall\", \"commands\": [<REAL command/"
        f"config for review>], \"why\": <<=10 words>}}], \"impact_nodes\": [<this ip, its /24, 192.168.1.1>], "
        f"\"confidence\": <0-1>}}."
    )
    payload = {
        "internal_host": ip,
        "denied_flows": lateral[1],
        "device": dev or {"ip": ip, "note": "not in exposed device set"},
        "external_campaign": {"distinct_sources": ev["distinctSrc"], "admin_lockouts": ev["lockouts"]},
        "top_denied_ports": ev["denyPorts"][:6],
    }
    try:
        out = client.complete_json([{"role": "user", "content": instr + "\n" + json.dumps(payload)}], schema_name="internal_host")
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True, "ip": ip, "attempts": lateral[1], "netblock": (dev or {}).get("cidr", ""),
        "verdict": out.get("verdict", ""), "severity": out.get("severity", "high"),
        "campaign": out.get("campaign", ""), "killChain": out.get("kill_chain", ""),
        "attribution": out.get("attribution", ""), "blast": out.get("blast", ""),
        "siblings": [], "internalCorrelation": [],
        "actions": [a for a in (out.get("actions") or []) if a][:4],
        "playbook": [
            {"target": p.get("target", ""), "targetIp": p.get("targetIp", ""), "layer": p.get("layer", ""),
             "commands": [c for c in (p.get("commands") or []) if c][:6], "why": p.get("why", "")}
            for p in (out.get("playbook") or []) if isinstance(p, dict) and p.get("commands")
        ][:6],
        "impactNodes": [x for x in (out.get("impact_nodes") or []) if x][:12],
        "confidence": out.get("confidence"), "lockouts": ev["lockouts"], "distinctSrc": ev["distinctSrc"],
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
