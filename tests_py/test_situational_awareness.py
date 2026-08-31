from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert result["mode"] == "production_observed"
    assert result["inventory"]["knownAssets"] == 2
    assert result["inventory"]["active24h"] == 2
    assert result["behaviorDeviations"][0]["asset"] == "192.168.1.4"
    assert result["crossSegment"]["records"][0]["action"] == "deny"
    assert result["candidatePaths"][0]["state"] == "blocked"
    assert result["candidatePaths"][0]["steps"][1]["label"] == "策略 8 · deny"
    assert [case["caseId"] for case in result["cases"]] == ["case-real"]
    assert result["funnel"]["cases"] == 1
    assert result["funnel"]["facts"] == 1000
    assert result["effectMeasurement"]["qualified"] is False
    blind = {item["capability"]: item["state"] for item in result["coverage"]}
    assert blind["same_segment_movement"] == "blind"
    assert blind["user_behavior"] == "blind"


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
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    assert result["inventory"]["knownAssets"] == 0
    assert result["crossSegment"]["records"] == []
    assert result["candidatePaths"] == []
    assert result["externalSources"] == []
    assert result["intelFeed"]["configured"] is False
