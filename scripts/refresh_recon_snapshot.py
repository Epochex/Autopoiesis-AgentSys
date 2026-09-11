#!/usr/bin/env python3
"""Refresh the internal-network recon surface with a read-only TCP-connect sweep.

The NETWORK CONTEXT page used to draw its "open ports" surface from a capture
committed on 2026-07-26; the rest of the page is live, and the label said so.
This sweep keeps the surface current under the same guardrails as that capture:

  * allowlisted targets only — the hosts already in the committed snapshot plus
    the addresses the situational inventory currently sees in 192.168.1.0/24;
  * the same fixed port set the snapshot was taken with — no discovery scans;
  * TCP connect only: no banner grabbing, no credentials, no exploit probes,
    nothing written to any device. A banner or TLS note is carried over from
    the committed capture when the same port is still open, never re-read.

Output goes to /data/autopoiesis-production/recon/live_surface.json, which the
recon report prefers when it is less than a day old. Run by the
autopoiesis-recon-refresh.timer every 30 minutes, or by hand.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "domains" / "active_recon" / "fixtures" / "real_surface_snapshot.json"
OUTPUT = Path(os.getenv("AUTOPOIESIS_RECON_LIVE_PATH", "/data/autopoiesis-production/recon/live_surface.json"))
GATEWAY = os.getenv("AUTOPOIESIS_GATEWAY", "http://127.0.0.1:8026")
CIDR = ipaddress.ip_network("192.168.1.0/24")
TIMEOUT = float(os.getenv("AUTOPOIESIS_RECON_TIMEOUT_SEC", "0.35"))
WORKERS = 64
SERVICE_BY_PORT = {
    21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http", 135: "msrpc", 139: "netbios-ssn",
    443: "https", 445: "microsoft-ds", 554: "rtsp", 3306: "mysql", 3389: "rdp", 5000: "upnp",
    5432: "postgresql", 6379: "redis", 8000: "http-alt", 8080: "http-proxy", 8443: "https-alt",
    9000: "cslistener", 9100: "jetdirect", 9200: "elasticsearch",
}


def committed_surface() -> dict[str, list[dict]]:
    try:
        return json.loads(FIXTURE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def inventory_addresses() -> set[str]:
    """Addresses the console currently sees; a host that appeared since the capture is swept too."""
    try:
        with urllib.request.urlopen(f"{GATEWAY}/api/rca/situational-overview", timeout=60) as response:
            overview = json.loads(response.read().decode("utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    for asset in (overview.get("inventory") or {}).get("assets") or []:
        ip = str(asset.get("ip") or "")
        try:
            if ipaddress.ip_address(ip) in CIDR:
                out.add(ip)
        except ValueError:
            continue
    return out


def connect(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def main() -> int:
    committed = committed_surface()
    ports = sorted({int(item["port"]) for rows in committed.values() for item in rows} | set(SERVICE_BY_PORT))
    hosts = {ip for ip in committed if ipaddress.ip_address(ip) in CIDR} | inventory_addresses()
    hosts = sorted(hosts, key=lambda ip: ipaddress.ip_address(ip))
    targets = [(ip, port) for ip in hosts for port in ports]
    started = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        opened = [target for target, ok in zip(targets, pool.map(lambda t: connect(*t), targets)) if ok]
    surface: dict[str, list[dict]] = {}
    for ip, port in opened:
        prior = next((item for item in committed.get(ip, []) if int(item.get("port") or 0) == port), None)
        surface.setdefault(ip, []).append({
            "port": port,
            "svc": (prior or {}).get("svc") or SERVICE_BY_PORT.get(port, f"tcp/{port}"),
            "banner": (prior or {}).get("banner"),
            "tls": (prior or {}).get("tls"),
        })
    payload = {
        "schema_version": 1,
        "captured_at": started.isoformat(),
        "cidr": str(CIDR),
        "mode": "allowlisted_readonly_tcp_connect",
        "guardrails": {"target_discovery": False, "banner_grab": False, "credential_probe": False,
                       "exploit_probe": False, "configuration_write": False},
        "hosts_checked": len(hosts),
        "ports_checked": ports,
        "duration_sec": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        "surface": surface,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".live_surface.", dir=OUTPUT.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(temporary, OUTPUT)
    print(f"recon surface refreshed: {len(hosts)} hosts × {len(ports)} ports in {payload['duration_sec']}s → "
          f"{len(surface)} hosts with {len(opened)} open ports → {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
