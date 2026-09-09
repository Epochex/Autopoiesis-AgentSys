from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any

from frontend.gateway.app import situational_awareness
from frontend.gateway.app.situational_awareness import build_situational_overview


@dataclass
class _Case:
    case_id: str
    subject: str
    status: str = "escalated"
    severity: str = "critical"
    service: str = "duplicate_ip_static"
    rule_id: str = "l2_ownership_drift"
    scope: str = "192.168.1.0/24"
    title: str = "address conflict"
    summary: str = "address owner changed"
    first_seen_at: str = "2026-08-31T10:00:00+00:00"
    last_seen_at: str = "2026-08-31T11:00:00+00:00"
    occurrence_count: int = 1
    source_payload: dict[str, Any] = field(default_factory=lambda: {
        "dataClassification": "observed",
        "incidentFacts": {"dataClassification": "observed"},
    })
    timeline: tuple[dict[str, Any], ...] = ({
        "kind": "business_decision_recorded",
        "ts": "2026-08-31T10:10:00+00:00",
        "decision": {
            "headline": "地址归属持续冲突",
            "summary": "三次复核仍观察到两个 MAC 争用地址",
            "classification": "duplicate_ip_static",
            "action": "定位交换机端口",
            "evidence": [{"evidenceId": "l2:1"}],
        },
    },)

    def latest_event(self, kind: str) -> dict[str, Any] | None:
        return next((row for row in reversed(self.timeline) if row.get("kind") == kind), None)


class _Repository:
    def list(self, limit: int = 500):
        return [
            _Case("case-real", "192.168.1.4"),
            _Case("case-test", "bvaccept-fail-123.service"),
        ]


class _Router:
    def __init__(self, **_options: Any) -> None:
        pass

    def fetch_all(self) -> dict[str, Any]:
        return {
            "fetched_at": "2026-08-31T11:59:00+00:00",
            "degraded": False,
            "interfaces": {"items": [
                {"name": "users", "status": "up", "ip": "192.168.1.1 255.255.255.0", "role": "lan", "vlan_id": 1},
                {"name": "servers", "status": "up", "ip": "192.168.16.1 255.255.255.0", "role": "lan", "vlan_id": 16},
            ]},
            "devices": {"items": [
                {"ip": "192.168.1.4", "mac": "00:11:22:33:44:55", "hostname": "camera-4", "sources": ["dhcp_lease"]},
                {"ip": "192.168.16.27", "mac": "00:11:22:33:44:66", "hostname": "node-27", "sources": ["known_device"]},
            ]},
            "policies": {"items": [
                {"id": 8, "source_zones": ["users"], "destination_zones": ["servers"], "action": "deny"},
            ]},
            "changes": {"items": []},
        }


def test_gateway_interface_aliases_do_not_become_terminal_or_public_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_INTEL_FEED", str(tmp_path / "missing.jsonl"))
    class Router(_Router):
        def fetch_all(self):
            snapshot = super().fetch_all()
            snapshot['identity'] = {'items': [{'serial': 'FG-OWNED', 'model': 'FG100E'}]}
            snapshot['interfaces']['items'].append({'name': 'wan1', 'ip': '77.236.99.122 255.255.255.248', 'role': 'wan'})
            snapshot['devices']['items'].append({'ip': '192.168.1.1', 'hostname': 'gateway', 'sources': ['known_device']})
            return snapshot
    def query(sql):
        rows = _query(sql)
        if 'internet_outbound' in sql:
            rows.append(dict(rows[0], dstip='77.236.99.122'))
        return rows
    result = build_situational_overview(_Repository(), {}, query=query, router_factory=Router,
                                       identity_provider=lambda: {}, now=datetime(2026, 9, 9, tzinfo=timezone.utc))
    assert result['inventory']['managedDevices'][0]['serial'] == 'FG-OWNED'
    assert '192.168.1.1' not in {a['ip'] for a in result['inventory']['assets']}
    assert '77.236.99.122' not in {r['destination'] for r in result['internetOutbound']['records']}


def _query(sql: str) -> list[dict[str, Any]]:
    if "count() AS retained" in sql:
        return [{"latest": "2026-08-31 11:59:50.000", "retained": 1000}]
    if "anyLast(srcname)" in sql:
        return [
            {"srcip": "192.168.1.4", "srcname": "camera-4", "srcintf": "users", "flows": 300,
             "bytes": 9000, "denied": 280, "peers": 12, "services": ["SSH"], "last_seen": "2026-08-31 11:59:50.000"},
            {"srcip": "192.168.16.27", "srcname": "node-27", "srcintf": "servers", "flows": 20,
             "bytes": 1000, "denied": 0, "peers": 2, "services": ["HTTPS"], "last_seen": "2026-08-31 11:58:00.000"},
        ]
    if "baseline_flows" in sql:
        return [{"srcip": "192.168.1.4", "current_flows": 300, "current_denied": 280,
                 "current_peers": 12, "current_ports": 4, "baseline_flows": 40,
                 "baseline_denied": 5, "baseline_peers": 2}]
    if "internet_outbound" in sql:
        return [{"srcip": "192.168.1.4", "dstip": "8.8.4.4", "action": "accept",
                 "srcintf": "users", "dstintf": "wan1", "service": "HTTPS", "dstport": 443,
                 "country": "United States", "flows": 4, "bytes": 4096,
                 "first_seen": "2026-08-31 11:30:00.000", "last_seen": "2026-08-31 11:59:00.000"}]
    if "GROUP BY srcip,dstip,action" in sql:
        return [{"srcip": "192.168.1.4", "dstip": "192.168.16.27", "action": "deny",
                 "srcintf": "users", "dstintf": "servers", "service": "SSH", "dstport": 22,
                 "flows": 30, "bytes": 0, "first_seen": "2026-08-31 11:00:00.000",
                 "last_seen": "2026-08-31 11:59:00.000"}]
    if "GROUP BY srcip ORDER BY unique_events" in sql:
        return [{"srcip": "8.8.8.8", "events": 10, "unique_events": 10,
                 "event_types": ["management_probe"], "ports": [443],
                 "last_seen": "2026-08-31 11:57:00.000"}]
    if "AS security_events" in sql:
        return [{"facts": 1000, "security_events": 10, "alerts": 2}]
    raise AssertionError(sql)


def test_projection_uses_live_sources_and_keeps_test_cases_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_INTEL_FEED", str(tmp_path / "missing.jsonl"))
    report = {
        "checked_at": "2026-08-31T11:59:00+00:00",
        "findings": [{
            "finding_id": "env-1", "subject": "192.168.1.4", "severity": "critical",
            "fault_class": "duplicate_ip_static", "headline": "地址归属在两个 MAC 之间切换",
            "verification": {"state": "confirmed", "source": "l2_identity_history", "checked_at": "2026-08-31T11:59:00+00:00"},
        }],
    }
    result = build_situational_overview(
        _Repository(), report, query=_query, router_factory=_Router,
        identity_provider=lambda: {
            "byMac": {"00:11:22:33:44:55": {
                "hostname": "IPC-HFW-4", "vendor": "Dahua", "os": None,
                "type": "IP Camera", "family": "IPC",
            }},
            "byIp": {}, "fresh": True,
        },
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert result["mode"] == "production_observed"
    assert result["inventory"]["knownAssets"] == 2
    fingerprinted = next(a for a in result["inventory"]["assets"] if a["ip"] == "192.168.1.4")
    assert fingerprinted["name"] == "IPC-HFW-4"
    assert fingerprinted["deviceClass"] == "camera"
    assert fingerprinted["identity"] == {"vendor": "Dahua", "type": "IP Camera", "family": "IPC"}
    untyped = next(a for a in result["inventory"]["assets"] if a["ip"] == "192.168.16.27")
    assert "deviceClass" not in untyped and "identity" not in untyped
    assert result["inventory"]["active24h"] == 2
    assert result["behaviorDeviations"][0]["asset"] == "192.168.1.4"
    assert result["crossSegment"]["records"][0]["action"] == "deny"
    assert result["candidatePaths"][0]["state"] == "blocked"
    assert result["candidatePaths"][0]["steps"][1]["label"] == "策略 8 · deny"
    assert [case["caseId"] for case in result["cases"]] == ["case-real"]
    assert result["funnel"]["cases"] == 1
    assert result["funnel"]["facts"] == 1000
    assert result["internetOutbound"]["records"][0]["destination"] == "8.8.4.4"
    assert result["internetOutbound"]["records"][0]["destinationInterface"] == "wan1"
    assert result["effectMeasurement"]["qualified"] is False
    blind = {item["capability"]: item["state"] for item in result["coverage"]}
    assert blind["same_segment_movement"] == "blind"
    assert blind["user_behavior"] == "blind"


def test_internet_query_excludes_zero_byte_denies() -> None:
    seen: list[str] = []

    def query(sql: str) -> list[dict[str, Any]]:
        seen.append(sql)
        return []

    assert situational_awareness._internet_outbound_rows(query) == []
    assert "(sentbyte + rcvdbyte) > 0" in seen[0]
    assert "lower(action) NOT IN ('deny', 'blocked', 'reject')" in seen[0]
    assert "GROUP BY srcip,dstip" in seen[0]
    assert "LIMIT 20 BY srcip" not in seen[0]


def test_empty_optional_sources_stay_empty_instead_of_becoming_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_INTEL_FEED", str(tmp_path / "missing.jsonl"))

    def empty_query(sql: str) -> list[dict[str, Any]]:
        return []

    class EmptyRouter:
        def __init__(self, **_options: Any) -> None:
            pass

        def fetch_all(self) -> dict[str, Any]:
            return {
                "fetched_at": None, "degraded": True,
                "interfaces": {"items": []}, "devices": {"items": []},
                "policies": {"items": []}, "changes": {"items": []},
            }

    result = build_situational_overview(
        _Repository(), None, query=empty_query, router_factory=EmptyRouter,
        identity_provider=lambda: {"byIp": {}, "byMac": {}, "fresh": False},
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    assert result["inventory"]["knownAssets"] == 0
    assert result["crossSegment"]["records"] == []
    assert result["candidatePaths"] == []
    assert result["externalSources"] == []
    assert result["intelFeed"]["configured"] is False


def test_local_host_readback_wins_over_stale_router_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_INTEL_FEED", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(situational_awareness, "_local_host_identity", lambda: {
        "ip": "192.168.16.27", "hostname": "netops-node1", "os": "Linux",
    })

    result = build_situational_overview(
        _Repository(), None, query=_query, router_factory=_Router,
        identity_provider=lambda: {
            "byMac": {},
            "byIp": {"192.168.16.27": {
                "hostname": "netops-node2", "vendor": "Apple", "os": "macOS",
                "type": "Computer", "family": "Mac",
            }},
            "fresh": True,
        },
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    local = next(a for a in result["inventory"]["assets"] if a["ip"] == "192.168.16.27")
    assert local["name"] == "netops-node1"
    assert local["identity"] == {"os": "Linux"}
    assert local["deviceClass"] == "server"
    assert local["sources"] == ["known_device", "local_host_readback"]
    assert local["identityConflict"] == {
        "source": "fortigate-live",
        "reported": {"hostname": "netops-node2", "os": "macOS"},
        "resolvedBy": "local_host_readback",
    }


def test_independent_source_queries_are_collected_concurrently(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_INTEL_FEED", str(tmp_path / "missing.jsonl"))
    started = 0
    started_lock = threading.Lock()
    all_started = threading.Event()

    def synchronized_empty_query(_sql: str) -> list[dict[str, Any]]:
        nonlocal started
        with started_lock:
            started += 1
            if started == 7:
                all_started.set()
        assert all_started.wait(timeout=1.0), "source queries ran serially"
        return []

    class EmptyRouter:
        def __init__(self, **_options: Any) -> None:
            pass

        def fetch_all(self) -> dict[str, Any]:
            return {
                "fetched_at": None, "degraded": True,
                "interfaces": {"items": []}, "devices": {"items": []},
                "policies": {"items": []}, "changes": {"items": []},
            }

    result = build_situational_overview(
        _Repository(), None, query=synchronized_empty_query, router_factory=EmptyRouter,
        identity_provider=lambda: {},
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert started == 7
    assert result["ok"] is True


def test_last_good_projection_survives_process_cache_reset(tmp_path, monkeypatch):
    cache_path = tmp_path / "situational-overview.json"
    payload = {
        "ok": True,
        "mode": "production_observed",
        "observedAt": "2026-09-07T08:00:00+00:00",
        "inventory": {"assets": []},
    }
    monkeypatch.setattr(situational_awareness, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(situational_awareness, "_cache", None)

    situational_awareness._persist_situational_overview(payload)
    restored, age = situational_awareness.cached_situational_overview()

    assert restored == payload
    assert age is not None and age >= 0
