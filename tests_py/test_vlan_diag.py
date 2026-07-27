"""Falsifiable tests for the per-VLAN / per-node diagnosis module.

Two clearly-separated layers, tested without any live dependency:

  * Layer 1 (passive congestion aggregate) — ``vlan_diag._q`` is monkeypatched
    to canned rows; we pin the aggregate shape, the denyRate math and the empty
    path. No ClickHouse contacted.
  * Layer 2 (active ICMP probe) — ``subprocess.run`` is monkeypatched to a canned
    ping stdout; we pin the rtt_min/avg/max/jitter/loss parsing, the
    FileNotFoundError degraded path (must not raise), and IPv4 validation.
"""
from __future__ import annotations

import pytest

from frontend.gateway.app import vlan_diag as vd


# ---------------------------------------------------------------------------
# Layer 1 — passive aggregate
# ---------------------------------------------------------------------------

_TOTALS = [{
    "flows": 1000, "up": 500, "down": 1500, "bytes": 2000,
    "deny": 250, "accept": 750, "peers": 40, "devices": 12,
}]
_NODES = [
    {"ip": "192.168.16.6", "flows": 400, "up": 100, "down": 300,
     "deny": 100, "accept": 300, "peers": 20, "topPorts": [443, 80, 53, 22]},
    {"ip": "192.168.16.7", "flows": 100, "up": 40, "down": 60,
     "deny": 0, "accept": 100, "peers": 5, "topPorts": [443]},
]


def _fake_q_factory(totals, nodes):
    def _fake_q(sql: str):
        # The totals query has no GROUP BY; the per-node query does.
        return nodes if "GROUP BY" in sql else totals
    return _fake_q


def test_vlan_diag_aggregate_shape_and_denyrate(monkeypatch):
    monkeypatch.setattr(vd, "_q", _fake_q_factory(_TOTALS, _NODES))
    # Cache is keyed on prefix+days; clear so we exercise the query path.
    vd._cache.clear()
    out = vd.vlan_diag("192.168.16.0/24", days=7)
    assert out is not None
    assert out["ok"] is True
    assert out["cidr"] == "192.168.16.0/24"
    assert out["days"] == 7
    assert out["signal"] == "passive"
    assert "非网络延迟" in out["note"]

    t = out["totals"]
    assert t["devices"] == 12
    assert t["flows"] == 1000
    assert t["up"] == 500 and t["down"] == 1500 and t["bytes"] == 2000
    assert t["deny"] == 250 and t["accept"] == 750
    assert t["peers"] == 40
    # denyRate = deny / max(1, flows) rounded to 3dp = 250/1000 = 0.25
    assert t["denyRate"] == 0.25

    assert len(out["nodes"]) == 2
    n0 = out["nodes"][0]
    assert n0["ip"] == "192.168.16.6"
    assert n0["denyRate"] == 0.25  # 100/400
    assert n0["topPorts"] == [443, 80, 53, 22]
    # zero-flow-safe node math
    assert out["nodes"][1]["denyRate"] == 0.0  # 0/100


def test_vlan_diag_empty_path(monkeypatch):
    monkeypatch.setattr(vd, "_q", _fake_q_factory([{"flows": 0}], []))
    vd._cache.clear()
    out = vd.vlan_diag("10.0.0.0/24", days=3)
    assert out == {"ok": True, "cidr": "10.0.0.0/24", "days": 3, "empty": True}


def test_vlan_diag_rejects_bad_cidr(monkeypatch):
    monkeypatch.setattr(vd, "_q", _fake_q_factory(_TOTALS, _NODES))
    vd._cache.clear()
    assert vd.vlan_diag("not-a-cidr") is None
    assert vd.vlan_diag("192.168.16.0") is None  # no /mask
    assert vd.vlan_diag("999.1.1.0/24") is None  # octet out of range


def test_vlan_diag_returns_none_on_ch_failure(monkeypatch):
    def _boom(sql):
        raise RuntimeError("clickhouse down")
    monkeypatch.setattr(vd, "_q", _boom)
    vd._cache.clear()
    assert vd.vlan_diag("192.168.16.0/24") is None


# ---------------------------------------------------------------------------
# Layer 2 — active ICMP probe
# ---------------------------------------------------------------------------

_PING_OK = (
    "PING 192.168.16.6 (192.168.16.6) 56(84) bytes of data.\n"
    "64 bytes from 192.168.16.6: icmp_seq=1 ttl=64 time=0.512 ms\n"
    "\n--- 192.168.16.6 ping statistics ---\n"
    "5 packets transmitted, 5 received, 0% packet loss, time 812ms\n"
    "rtt min/avg/max/mdev = 0.402/0.531/0.688/0.101 ms\n"
)

_PING_LOSS = (
    "PING 192.168.16.9 (192.168.16.9) 56(84) bytes of data.\n"
    "\n--- 192.168.16.9 ping statistics ---\n"
    "5 packets transmitted, 0 received, 100% packet loss, time 4090ms\n"
)


class _Proc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def test_probe_latency_parses_rtt_and_loss(monkeypatch):
    monkeypatch.setattr(vd.subprocess, "run", lambda *a, **k: _Proc(_PING_OK))
    out = vd.probe_latency(["192.168.16.6"], count=5)
    assert out["ok"] is True
    assert out["probe"] == "icmp"
    assert out["signal"] == "active"
    assert out["count"] == 5
    assert out["degraded"] is False
    r = out["results"][0]
    assert r["ip"] == "192.168.16.6"
    assert r["reachable"] is True
    assert r["loss_pct"] == 0.0
    assert r["rtt_min"] == 0.402
    assert r["rtt_avg"] == 0.531
    assert r["rtt_max"] == 0.688
    assert r["jitter"] == 0.101


def test_probe_latency_full_loss_unreachable(monkeypatch):
    monkeypatch.setattr(vd.subprocess, "run", lambda *a, **k: _Proc(_PING_LOSS))
    out = vd.probe_latency(["192.168.16.9"])
    r = out["results"][0]
    assert r["reachable"] is False
    assert r["loss_pct"] == 100.0
    assert r["rtt_avg"] is None


def test_probe_latency_ping_absent_degraded(monkeypatch):
    def _no_ping(*a, **k):
        raise FileNotFoundError("ping")
    monkeypatch.setattr(vd.subprocess, "run", _no_ping)
    out = vd.probe_latency(["192.168.16.6"])  # must not raise
    assert out["ok"] is True
    assert out["degraded"] is True
    r = out["results"][0]
    assert r["reachable"] is False
    assert r["loss_pct"] is None


def test_probe_latency_rejects_junk_ips(monkeypatch):
    monkeypatch.setattr(vd.subprocess, "run", lambda *a, **k: _Proc(_PING_OK))
    out = vd.probe_latency(["not-an-ip", "999.999.1.1", "1.2.3", ""])
    assert out["results"] == []  # all rejected by validation
    assert out["ok"] is True


def test_probe_latency_caps_count():
    assert vd.probe_latency([], count=99)["count"] == 10
    assert vd.probe_latency([], count=0)["count"] == 1
