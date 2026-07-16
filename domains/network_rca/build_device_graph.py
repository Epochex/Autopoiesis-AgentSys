"""Build the per-subnet DEVICE GRAPH from the real FortiGate syslog.

The gateway only forwards L3 traffic, so device-to-device links inside a /24 are
never logged directly. Everything a topology can honestly show about the inner
life of a subnet has to be *reconstructed* from the traces the firewall does
emit: DHCP leases, MAC/OUI, hostnames, broadcast targets, session clashes and
the destinations each host talks to.

This module mines exactly those signals and emits, per subnet:

  devices[]  every host ever seen on the segment (not just the noisy few),
             with vendor, role, lease cadence, deny/accept, ports.
  edges[]    device<->device relations, each carrying its evidence kind:
               clash   duplicate-IP / session-clash tuples  (hard evidence)
               bcast   same broadcast/discovery domain      (hard evidence)
               codst   same destination host/service        (hard evidence)
               fleet   same MAC OUI vendor block            (inferred)
               family  same hostname family                 (inferred)
               lease   DHCP leases granted in lockstep      (inferred)
               portfp  identical destination-port fingerprint (inferred)
  clusters[] connected components over the strong edges, with a role label.
  layout     deterministic force-directed x/y in [-1, 1] so the UI can render
             ~150 nodes without shipping a physics engine to the browser.

Run:  python3 -m domains.network_rca.build_device_graph
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REAL = _HERE / "fixtures" / "real"
_OUT = _REAL / "real_device_graph.json"

_KV = re.compile(r'(\w+)=("[^"]*"|\S+)')
_CLASH_IP = re.compile(r"(192\.168\.\d+\.\d+):\d+->")
_PRIVATE = ("192.168.", "10.", "172.16.", "172.17.")
_BCAST_SUFFIX = (".255",)
_MCAST_PREFIX = ("224.", "239.", "255.")

# Common OUI blocks in this deployment. Unknown blocks stay honest ("unknown").
_OUI: dict[str, str] = {
    "F4:B1:C2": "Dahua", "40:7A:A4": "Dahua", "3C:EF:8C": "Dahua", "E0:50:8B": "Dahua",
    "D0:39:57": "Dahua", "94:B6:09": "Dahua", "84:15:D3": "Dahua", "A8:6D:AA": "Intel",
    "C4:AA:C4": "TP-Link", "FC:5F:49": "Xiaomi", "60:E9:AA": "Espressif",
    "D4:AB:61": "Dell", "D4:43:0E": "Cisco", "A8:16:9D": "Samsung", "0A:05:96": "Apple",
}
# Dahua intercom / camera naming, seen verbatim in DHCP hostnames + srcname.
_ROLE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(vto|vth)", re.I), "intercom"),
    (re.compile(r"^(es-|ipc|nvr|dvr|dh-)", re.I), "camera"),
    (re.compile(r"(iphone|ipad|galaxy|xiaomi|vivo|redmi|honor|huawei|noh-|oppo|android)", re.I), "mobile"),
    (re.compile(r"(desktop-|laptop-|-pc$|^win|^mh$|^hub)", re.I), "workstation"),
    (re.compile(r"(server|nas|srv|r230)", re.I), "server"),
]


def _parse(line: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _KV.findall(line)}


def _cidr_of(ip: str) -> str:
    p = ip.split(".")
    return ".".join(p[:3]) + ".0/24" if len(p) == 4 else ""


def _internal(ip: str) -> bool:
    return ip.startswith(_PRIVATE)


def _is_bcast(ip: str) -> bool:
    return ip.endswith(_BCAST_SUFFIX) or ip.startswith(_MCAST_PREFIX)


def _vendor(mac: str | None) -> str:
    if not mac:
        return "unknown"
    return _OUI.get(mac.upper()[:8], "unknown")


def _role(name: str | None, os_: str | None, ports: Counter) -> str:
    for pat, role in _ROLE_RULES:
        if name and pat.search(name):
            return role
    if os_ in ("Android", "iOS"):
        return "mobile"
    if os_ in ("Windows", "Linux"):
        return "workstation"
    if any(p in ports for p in ("554", "37777", "3702")):
        return "camera"
    return "unknown"


def _family(name: str | None) -> str:
    if not name:
        return ""
    base = re.sub(r"[-_\s]?[0-9A-Fa-f]{4,}$", "", name.strip())
    base = re.sub(r"[-_\s]?\d+$", "", base)
    return base[:14].lower()


def _secs(t: str | None) -> int:
    if not t or ":" not in t:
        return -1
    try:
        h, m, s = (int(x) for x in t.split(":"))
    except ValueError:
        return -1
    return h * 3600 + m * 60 + s


def _threat(deny: int, accept: int, ports: Counter) -> str:
    if deny >= 20000 or (deny > 4000 and len(ports) > 6):
        return "high"
    if deny >= 2000:
        return "watch"
    return "ok"


def mine(log_paths: list[Path]) -> dict[str, Any]:
    dev: dict[str, dict[str, Any]] = {}
    clash_pairs: Counter[tuple[str, str]] = Counter()
    bcast: dict[str, set[str]] = defaultdict(set)      # bcast target -> senders
    dst_hosts: dict[str, set[str]] = defaultdict(set)  # unicast dst -> senders
    ext_hits: Counter[str] = Counter()

    def slot(ip: str) -> dict[str, Any]:
        return dev.setdefault(ip, {
            "ip": ip, "mac": None, "name": None, "os": None, "intf": None,
            "dhcp": 0, "flows": 0, "deny": 0, "accept": 0,
            "ports": Counter(), "dsts": Counter(), "leases": [],
        })

    for path in log_paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in line:
                continue
            d = _parse(line)
            ip = d.get("ip", "")
            if d.get("logdesc") == "DHCP Ack log" and _internal(ip):
                e = slot(ip)
                e["dhcp"] += 1
                e["mac"] = d.get("mac") or e["mac"]
                e["intf"] = d.get("interface") or e["intf"]
                host = d.get("hostname")
                if host and host != "N/A":
                    e["name"] = host
                ts = _secs(d.get("time"))
                if ts >= 0:
                    e["leases"].append(ts)

            if d.get("logdesc") == "session clash":
                ips = sorted({x for x in _CLASH_IP.findall(line)})
                for i, a in enumerate(ips):
                    for b in ips[i + 1:]:
                        if _cidr_of(a) == _cidr_of(b):
                            clash_pairs[(a, b)] += 1
                            slot(a), slot(b)

            if d.get("type") != "traffic":
                continue
            src, dst = d.get("srcip", ""), d.get("dstip", "")
            if not _internal(src):
                if dst:
                    ext_hits[src] += 1
                continue
            e = slot(src)
            e["flows"] += 1
            e["mac"] = e["mac"] or d.get("srcmac")
            e["name"] = e["name"] or d.get("srcname")
            e["os"] = e["os"] or d.get("osname")
            e["intf"] = e["intf"] or d.get("srcintf")
            if d.get("action") == "deny":
                e["deny"] += 1
            else:
                e["accept"] += 1
            if d.get("dstport"):
                e["ports"][d["dstport"]] += 1
            if dst:
                e["dsts"][dst] += 1
                if _is_bcast(dst):
                    bcast[dst].add(src)
                elif _internal(dst):
                    dst_hosts[dst].add(src)

    return {"dev": dev, "clash": clash_pairs, "bcast": bcast, "dst_hosts": dst_hosts}


def _edges_for(cidr: str, ips: list[str], dev: dict, mined: dict) -> list[dict[str, Any]]:
    """Every device<->device relation the firewall telemetry can actually justify."""
    edges: Counter[tuple[str, str, str]] = Counter()
    note: dict[tuple[str, str, str], str] = {}
    ipset = set(ips)

    def add(a: str, b: str, kind: str, w: int, why: str) -> None:
        if a == b or a not in ipset or b not in ipset:
            return
        key = (min(a, b), max(a, b), kind)
        edges[key] += w
        note.setdefault(key, why)

    # 1. session clash — the same L3 tuple claimed by two hosts (dup IP / NAT reuse)
    for (a, b), n in mined["clash"].items():
        add(a, b, "clash", n, f"session clash ×{n}")

    # 2. broadcast / discovery domain — hosts shouting at the same broadcast target.
    #    A 192.168.16.x host broadcasting to 192.168.31.255 is a real netmask
    #    misconfiguration, not noise: it leaks discovery across the /16.
    for target, senders in mined["bcast"].items():
        mem = sorted(s for s in senders if s in ipset)
        # 255.255.255.255 and multicast are legitimate link-local broadcast. A DIRECTED
        # broadcast to another /24 (192.168.16.x → 192.168.31.255) is not: the host's
        # netmask is wider than its segment, so discovery leaks across the /16.
        leak = (
            target.endswith(".255")
            and not target.startswith(_MCAST_PREFIX)
            and _cidr_of(target) not in ("", cidr)
        )
        for i, a in enumerate(mem):
            for b in mem[i + 1:]:
                add(a, b, "bcast", 3 if leak else 1, f"{'mask-leak ' if leak else ''}broadcast → {target}")

    # 3. shared destination — two hosts converging on the same server/service
    for target, senders in mined["dst_hosts"].items():
        mem = sorted(s for s in senders if s in ipset)
        if len(mem) > 12:  # a gateway everyone talks to carries no information
            continue
        for i, a in enumerate(mem):
            for b in mem[i + 1:]:
                add(a, b, "codst", 2, f"both → {target}")

    # 4. hardware fleet — same OUI vendor block
    by_oui: dict[str, list[str]] = defaultdict(list)
    for ip in ips:
        mac = dev[ip].get("mac")
        if mac:
            by_oui[mac.upper()[:8]].append(ip)
    for oui, mem in by_oui.items():
        if not 2 <= len(mem) <= 10:
            continue
        v = _OUI.get(oui, "unknown")
        for i, a in enumerate(sorted(mem)):
            for b in sorted(mem)[i + 1:]:
                add(a, b, "fleet", 1, f"{v} OUI {oui}")

    # 5. hostname family — same naming scheme = same deployment batch
    by_fam: dict[str, list[str]] = defaultdict(list)
    for ip in ips:
        f = _family(dev[ip].get("name"))
        if f:
            by_fam[f].append(ip)
    for fam, mem in by_fam.items():
        if not 2 <= len(mem) <= 14:
            continue
        for i, a in enumerate(sorted(mem)):
            for b in sorted(mem)[i + 1:]:
                add(a, b, "family", 1, f"{fam}* family")

    # 6. DHCP lockstep — leases granted inside the same 30 s window, repeatedly, and
    #    for MOST of both hosts' renewals. Loose co-timing is just a busy DHCP server;
    #    a high overlap ratio is what actually implies a shared switch / power domain.
    stamps = {ip: sorted(dev[ip]["leases"]) for ip in ips if len(dev[ip]["leases"]) >= 3}
    keys = sorted(stamps)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            sa, sb = stamps[a], stamps[b]
            hits = 0
            j = 0
            for t in sa:
                while j < len(sb) and sb[j] < t - 30:
                    j += 1
                if j < len(sb) and abs(sb[j] - t) <= 30:
                    hits += 1
            ratio = hits / min(len(sa), len(sb))
            if hits >= 4 and ratio >= 0.6:
                add(a, b, "lease", min(hits, 6), f"DHCP lockstep ×{hits} ({int(ratio * 100)}%)")

    # 7. port fingerprint — identical destination-port signature
    fp: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for ip in ips:
        top = tuple(p for p, _ in dev[ip]["ports"].most_common(3))
        if len(top) >= 2:
            fp[tuple(sorted(top))].append(ip)
    for sig, mem in fp.items():
        if not 2 <= len(mem) <= 8:
            continue
        for i, a in enumerate(sorted(mem)):
            for b in sorted(mem)[i + 1:]:
                add(a, b, "portfp", 2, "ports " + " ".join(f":{s}" for s in sig))

    W = {"clash": 5.0, "bcast": 3.0, "codst": 2.4, "portfp": 1.8, "lease": 1.4, "family": 1.0, "fleet": 0.9}
    out = [
        {
            "src": a, "dst": b, "kind": k,
            "weight": round(W[k] * math.log10(n + 1) + W[k] * 0.35, 3),
            "hits": n, "evidence": note[(a, b, k)],
            "observed": k in ("clash", "bcast", "codst"),
        }
        for (a, b, k), n in edges.items()
    ]
    # keep the graph legible: cap each node's degree, keeping its strongest ties
    deg: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    for e in sorted(out, key=lambda x: -x["weight"]):
        if deg[e["src"]] >= 7 or deg[e["dst"]] >= 7:
            continue
        deg[e["src"]] += 1
        deg[e["dst"]] += 1
        kept.append(e)
    return kept


def _layout(ips: list[str], edges: list[dict], clusters: list[dict]) -> dict[str, list[float]]:
    """Deterministic force layout (Fruchterman–Reingold), seeded per cluster."""
    idx = {ip: i for i, ip in enumerate(ips)}
    n = len(ips)
    if n == 0:
        return {}
    cl_of = {ip: ci for ci, c in enumerate(clusters) for ip in c["members"]}
    ncl = max(len(clusters), 1)
    pos = []
    for i, ip in enumerate(ips):
        ci = cl_of.get(ip, ncl)
        a = 2 * math.pi * (ci / (ncl + 1)) + (i % 7) * 0.31
        r = 0.42 + ((i * 37) % 100) / 240.0
        pos.append([math.cos(a) * r, math.sin(a) * r])

    adj = [[] for _ in range(n)]
    for e in edges:
        if e["src"] in idx and e["dst"] in idx:
            adj[idx[e["src"]]].append((idx[e["dst"]], e["weight"]))
            adj[idx[e["dst"]]].append((idx[e["src"]], e["weight"]))

    # Bigger k + weaker gravity spread the communities out to FILL the disc instead
    # of collapsing into one central blob — the expanded segment should use the space.
    k = 2.35 / math.sqrt(n)
    for step in range(340):
        t = 0.12 * (1 - step / 340) + 0.002
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                d2 = dx * dx + dy * dy + 1e-4
                f = (k * k) / d2
                disp[i][0] += dx * f
                disp[i][1] += dy * f
                disp[j][0] -= dx * f
                disp[j][1] -= dy * f
        for i in range(n):
            for j, w in adj[i]:
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                d = math.sqrt(dx * dx + dy * dy) + 1e-4
                f = (d / k) * min(w, 3.0) * 0.55
                disp[i][0] -= dx / d * f
                disp[i][1] -= dy / d * f
        for i in range(n):
            # gentle gravity — enough to keep disconnected leaves on-canvas, weak
            # enough that repulsion pushes the whole graph out toward the rim
            disp[i][0] -= pos[i][0] * 0.32
            disp[i][1] -= pos[i][1] * 0.32
            dl = math.sqrt(disp[i][0] ** 2 + disp[i][1] ** 2) + 1e-6
            pos[i][0] += disp[i][0] / dl * min(dl, t)
            pos[i][1] += disp[i][1] / dl * min(dl, t)

    # normalize each axis independently so the cloud fills the full [-1,1] box
    mxx = max(abs(p[0]) for p in pos) or 1.0
    mxy = max(abs(p[1]) for p in pos) or 1.0
    return {ip: [round(pos[i][0] / mxx, 4), round(pos[i][1] / mxy, 4)] for ip, i in idx.items()}


def _clusters(ips: list[str], edges: list[dict], dev: dict) -> list[dict[str, Any]]:
    """Weighted label propagation — communities, not one giant connected blob.

    Connected components collapse into a single component as soon as one chatty
    host bridges two groups; label propagation keeps the internal structure that
    the eye (and the analyst) actually needs.
    """
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        adj[e["src"]].append((e["dst"], e["weight"]))
        adj[e["dst"]].append((e["src"], e["weight"]))

    label = {ip: ip for ip in ips}
    order = sorted(ips)
    for _ in range(24):
        moved = 0
        for ip in order:
            if not adj[ip]:
                continue
            score: Counter[str] = Counter()
            for nb, w in adj[ip]:
                score[label[nb]] += w
            # deterministic tie-break: heaviest weight, then lowest label
            best = min(score.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if best != label[ip]:
                label[ip] = best
                moved += 1
        if not moved:
            break

    comp: dict[str, list[str]] = defaultdict(list)
    for ip in ips:
        comp[label[ip]].append(ip)

    out = []
    for i, (_, mem) in enumerate(sorted(comp.items(), key=lambda kv: -len(kv[1]))):
        if len(mem) < 2:
            continue
        roles = Counter(dev[m]["role"] for m in mem)
        vendors = Counter(dev[m]["vendor"] for m in mem if dev[m]["vendor"] != "unknown")
        kinds = Counter(e["kind"] for e in edges if e["src"] in mem and e["dst"] in mem)
        role = roles.most_common(1)[0][0]
        vendor = vendors.most_common(1)[0][0] if vendors else ""
        out.append({
            "id": f"c{i}",
            "members": sorted(mem),
            "role": role,
            "vendor": vendor,
            "size": len(mem),
            "boundBy": [k for k, _ in kinds.most_common(3)],
            "deny": sum(dev[m]["deny"] for m in mem),
        })
    return out


def _authoritative() -> dict[str, dict[str, Any]]:
    """Per-host counters from the FULL R230 capture (the committed logs are a sample).

    real_topology.json / real_mesh.json were computed over the whole window, so their
    deny/accept/flow counts outrank anything re-derived from the sampled copies. Without
    this overlay the flagged hosts would silently lose two orders of magnitude.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        topo = json.loads((_REAL / "real_topology.json").read_text(encoding="utf-8"))
        for sub in topo.get("subnets", []):
            for d in sub.get("devices", []) or []:
                out[d["ip"]] = {
                    "flows": d.get("flows", 0), "deny": d.get("deny", 0),
                    "accept": d.get("accept", 0), "ports": d.get("top_ports", []),
                    "threat": d.get("threat"),
                }
    except Exception:
        pass
    try:
        meshes = json.loads((_REAL / "real_mesh.json").read_text(encoding="utf-8")).get("meshes", {})
        for nodes in meshes.values():
            for n in nodes:
                cur = out.setdefault(n["ip"], {})
                cur.setdefault("flows", n.get("out", 0))
                cur.setdefault("deny", n.get("deny", 0))
                cur.setdefault("accept", n.get("accept", 0))
                cur.setdefault("ports", n.get("ports", []))
                cur.setdefault("threat", n.get("threat"))
                if n.get("role"):
                    cur["srcRole"] = n["role"]
    except Exception:
        pass
    return out


def build(log_paths: list[Path] | None = None) -> dict[str, Any]:
    if log_paths is None:
        manifest = json.loads((_REAL / "manifest.json").read_text(encoding="utf-8"))
        log_paths = [_REAL / p for p in manifest.get("syslog_paths", [])]
    mined = mine(log_paths)
    dev = mined["dev"]
    auth = _authoritative()

    for ip, e in dev.items():
        a = auth.get(ip)
        if a:
            e["flows"] = max(e["flows"], a.get("flows", 0))
            e["deny"] = max(e["deny"], a.get("deny", 0))
            e["accept"] = max(e["accept"], a.get("accept", 0))
            for p in a.get("ports", []):
                e["ports"][str(p)] += e["ports"].get(str(p), 0) or 1
        e["vendor"] = _vendor(e["mac"])
        e["role"] = _role(e["name"], e["os"], e["ports"])
        e["threat"] = (a or {}).get("threat") or _threat(e["deny"], e["accept"], e["ports"])

    by_sub: dict[str, list[str]] = defaultdict(list)
    for ip in dev:
        by_sub[_cidr_of(ip)].append(ip)

    graphs: dict[str, Any] = {}
    for cidr, ips in by_sub.items():
        if len(ips) < 2:
            continue
        ips = sorted(ips, key=lambda x: [int(p) for p in x.split(".")])
        edges = _edges_for(cidr, ips, dev, mined)
        clusters = _clusters(ips, edges, dev)
        pos = _layout(ips, edges, clusters)
        devices = []
        for ip in ips:
            e = dev[ip]
            devices.append({
                "ip": ip,
                "name": (e["name"] or "").strip() or None,
                "mac": e["mac"],
                "vendor": e["vendor"],
                "os": e["os"],
                "role": e["role"],
                "intf": e["intf"],
                "flows": e["flows"],
                "deny": e["deny"],
                "accept": e["accept"],
                "leases": e["dhcp"],
                "topPorts": [p for p, _ in e["ports"].most_common(4)],
                "threat": e["threat"],
                "seenBy": "traffic" if e["flows"] else "dhcp",
                "x": pos[ip][0],
                "y": pos[ip][1],
            })
        anomalies = []
        leaks = sorted({e["src"] for e in edges if "mask-leak" in e["evidence"]}
                       | {e["dst"] for e in edges if "mask-leak" in e["evidence"]})
        if leaks:
            anomalies.append({
                "kind": "netmask-leak",
                "members": leaks,
                "detail": "hosts broadcast outside their own /24 — netmask is wider than the segment",
            })
        clashers = sorted({e["src"] for e in edges if e["kind"] == "clash"}
                          | {e["dst"] for e in edges if e["kind"] == "clash"})
        if clashers:
            anomalies.append({
                "kind": "session-clash",
                "members": clashers,
                "detail": "same L3 tuple claimed by multiple hosts — duplicate IP or NAT reuse",
            })
        graphs[cidr] = {
            "cidr": cidr,
            "devices": devices,
            "edges": edges,
            "clusters": clusters,
            "anomalies": anomalies,
            "stats": {
                "devices": len(devices),
                "withTraffic": sum(1 for d in devices if d["flows"]),
                "dhcpOnly": sum(1 for d in devices if not d["flows"]),
                "edges": len(edges),
                "observedEdges": sum(1 for e in edges if e["observed"]),
                "deny": sum(d["deny"] for d in devices),
                "roles": dict(Counter(d["role"] for d in devices)),
                "vendors": dict(Counter(d["vendor"] for d in devices)),
            },
        }
    return {"graphs": graphs}


if __name__ == "__main__":
    out = build()
    _OUT.write_text(json.dumps(out, indent=1, sort_keys=False), encoding="utf-8")
    for cidr, g in out["graphs"].items():
        s = g["stats"]
        print(f"{cidr:>18}  devices={s['devices']:3d} (traffic {s['withTraffic']:2d})  "
              f"edges={s['edges']:3d} (observed {s['observedEdges']:3d})  clusters={len(g['clusters'])}  "
              f"anomalies={[a['kind'] for a in g['anomalies']]}")
    print(f"\nwrote {_OUT}")
