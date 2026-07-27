"""The benchmark 态势 snapshot must be the REAL evolved memory graph mapped into the
EXACT live topology shapes, so the frontend can reuse the live TopologyCanvas.

Every node/edge/label comes from load_evolution()'s observatory.records; these tests
guard the contract the drilled TopologyCanvas relies on (drillSub === graph.cidr, a
tier vendor per device, fully-formed GraphDevices, edges that resolve to real nodes).
"""
from __future__ import annotations

import pytest

from frontend.gateway.app.rca_reader import build_bench_snapshot

# GraphDevice required (non-optional) keys — mirror of types.ts GraphDevice.
REQUIRED_DEVICE_KEYS = {
    "ip", "name", "mac", "vendor", "os", "role", "intf", "flows", "deny",
    "accept", "leases", "topPorts", "threat", "seenBy", "x", "y",
}
TIERS = {"episodic", "semantic", "procedural", "insight"}


@pytest.fixture(scope="module")
def snap() -> dict:
    s = build_bench_snapshot(4)
    assert s["ok"], s
    return s


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def test_cidr_matches_so_drill_state_engages(snap):
    graph = snap["graph"]
    # drillSub is set to graph.cidr in the frontend; TopologyCanvas only enters the
    # drilled device-constellation when drillSub === graph.cidr === subnet cidr.
    assert graph["cidr"] == "mem://autopoiesis"
    assert snap["cidr"] == "mem://autopoiesis"
    assert snap["topology"]["subnets"][0]["cidr"] == graph["cidr"]


def test_every_graph_device_is_fully_formed(snap):
    devices = snap["graph"]["devices"]
    assert devices, "expected at least one memory node"
    for dv in devices:
        missing = REQUIRED_DEVICE_KEYS - set(dv)
        assert not missing, f"{dv.get('ip')} missing {missing}"
        assert _is_number(dv["x"]) and _is_number(dv["y"]), dv["ip"]
        assert -1.0 <= dv["x"] <= 1.0 and -1.0 <= dv["y"] <= 1.0, dv["ip"]
        # vendor is the tier, which drives the live constellation's clustering.
        assert dv["vendor"] in TIERS, dv["vendor"]
        assert dv["role"] in TIERS
        assert dv["threat"] in {"high", "watch", "ok"}
        assert isinstance(dv["topPorts"], list)


def test_edges_reference_existing_nodes(snap):
    graph = snap["graph"]
    device_ids = {d["ip"] for d in graph["devices"]}
    assert graph["edges"], "the memory graph has links"
    for e in graph["edges"]:
        assert e["src"] in device_ids, e
        assert e["dst"] in device_ids, e  # records + any insight/link-target node
        assert e["kind"] in {"clash", "bcast", "codst", "fleet", "family", "lease", "portfp"}
        assert e["observed"] is True


def test_clusters_cover_the_tiers(snap):
    graph = snap["graph"]
    tiers_present = {d["vendor"] for d in graph["devices"]}
    cluster_ids = {c["id"] for c in graph["clusters"]}
    assert cluster_ids == tiers_present
    device_ids = {d["ip"] for d in graph["devices"]}
    for c in graph["clusters"]:
        assert c["id"] in TIERS
        assert set(c["members"]) <= device_ids
        assert c["size"] == len(c["members"])
        assert c["vendor"] == c["id"]


def test_stats_are_computed_from_the_real_nodes(snap):
    graph = snap["graph"]
    devices = graph["devices"]
    st = graph["stats"]
    assert st["devices"] == len(devices)
    assert st["withTraffic"] == len(devices)
    assert st["dhcpOnly"] == 0
    assert st["deny"] == 0
    assert st["edges"] == len(graph["edges"]) == st["observedEdges"]
    assert sum(st["vendors"].values()) == len(devices)
    # DataStats echoes the same real node count; nothing about R230 traffic leaks in.
    ds = snap["stats"]
    assert ds["source"] == "benchmark · evolved memory graph"
    assert ds["distinctSrc"] == len(devices)
    assert ds["denyCount"] == 0 and ds["lockouts"] == 0
