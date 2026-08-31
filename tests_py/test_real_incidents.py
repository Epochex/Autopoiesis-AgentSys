from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from core.evolve.replay_stream import _SCHEMA_FIELDS, produce_tagged_replay
from domains.network_rca.incidents import (
    IncidentDispositionUpdate,
    IncidentRepository,
    build_incident_replay,
    detect_dual_mac_window,
    detect_host_network_drift,
    detect_host_network_preflight,
)


INCIDENT_23 = "inc-192-168-1-23-dual-mac-arp-drift"
INCIDENT_27 = "inc-192-168-1-27-cloud-init-network-drift"


def _arp_event(seconds: int, ip: str, mac: str, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "timestamp": f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z",
        "ip": ip,
        "mac": mac,
        "source_kind": "observed",
    }


def test_dual_mac_detector_is_per_ip_and_uses_inclusive_window_boundary():
    events = [
        _arp_event(0, "192.168.1.23", "02:00:00:00:00:01", "e1"),
        _arp_event(300, "192.168.1.23", "02:00:00:00:00:02", "e2"),
        _arp_event(1, "192.168.1.24", "02:00:00:00:00:03", "e3"),
    ]
    result = detect_dual_mac_window(events, window_seconds=300)

    assert result["detected"] is True
    assert [finding["ip"] for finding in result["findings"]] == ["192.168.1.23"]
    assert result["findings"][0]["evidence_ids"] == ["e1", "e2"]
    assert result["source_kinds"] == ["observed"]
    assert result["current_online_observation"] is False


def test_dual_mac_detector_does_not_join_events_outside_window_or_repeat_mac():
    outside = [
        _arp_event(0, "192.168.1.23", "02:00:00:00:00:01", "e1"),
        {
            **_arp_event(1, "192.168.1.23", "02:00:00:00:00:02", "e2"),
            "timestamp": "2026-01-01T00:05:01Z",
        },
    ]
    repeated = [
        _arp_event(0, "192.168.1.23", "02:00:00:00:00:01", "e1"),
        _arp_event(30, "192.168.1.23", "02:00:00:00:00:01", "e2"),
    ]

    assert detect_dual_mac_window(outside, 300)["detected"] is False
    assert detect_dual_mac_window(repeated, 300)["detected"] is False


def test_host_network_preflight_warns_before_restart_and_readback_finds_drift():
    preflight = detect_host_network_preflight(
        expected_business_ips=["192.168.1.27"],
        cloud_init_enabled=True,
        cloud_init_network_disabled=False,
        persistent_business_ips=[],
        source_kind="simulated",
    )
    drift = detect_host_network_drift(
        expected_business_ips=["192.168.1.27"],
        actual_business_ips=[],
        source_kind="simulated",
    )

    assert preflight["detected"] is True
    assert preflight["risks"] == [
        "cloud_init_can_manage_network_on_restart",
        "expected_business_ip_absent_from_persistent_config",
    ]
    assert drift["detected"] is True
    assert drift["missing_business_ips"] == ["192.168.1.27"]
    assert preflight["observation_scope"] == "replay_simulation"


def test_host_network_detectors_stay_clear_for_persistent_expected_address():
    preflight = detect_host_network_preflight(
        expected_business_ips=["192.168.1.27"],
        cloud_init_enabled=True,
        cloud_init_network_disabled=True,
        persistent_business_ips=["192.168.1.27"],
    )
    drift = detect_host_network_drift(
        expected_business_ips=["192.168.1.27"],
        actual_business_ips=["192.168.1.27"],
    )

    assert preflight["detected"] is False
    assert drift["detected"] is False


def test_fixture_exposes_two_historical_incidents_with_honest_provenance():
    repository = IncidentRepository()
    summaries = repository.list()

    assert {item["id"] for item in summaries} == {INCIDENT_23, INCIDENT_27}
    assert all(item["classification"] == "historical_real_incident" for item in summaries)
    assert all(item["current_online_observation"] is False for item in summaries)

    dual_mac = repository.get(INCIDENT_23)
    assert {item["source_kind"] for item in dual_mac["evidence_timeline"]} == {
        "manual",
        "observed",
    }
    assert dual_mac["disposition"]["status"] == "awaiting_readback"
    assert dual_mac["disposition"]["readback"]["state"] == "not_collected"
    assert dual_mac["detection"]["observation_scope"] == "collected_evidence"

    cloud_init = repository.get(INCIDENT_27)
    assert {item["source_kind"] for item in cloud_init["evidence_timeline"]} == {
        "manual",
        "observed",
        "readback",
    }
    assert cloud_init["disposition"]["status"] == "awaiting_readback"
    assert cloud_init["disposition"]["readback"]["source_kind"] == "readback"
    assert cloud_init["disposition"]["readback"]["state"] == "partial"

    for incident in (dual_mac, cloud_init):
        evidence_ids = {item["id"] for item in incident["evidence"]}
        for metric in incident["metrics"]:
            for point in metric["points"]:
                assert point.get("evidenceIds") or point.get("missingReason")
                assert set(point.get("evidenceIds") or []).issubset(evidence_ids)
        for node in incident["topology"]["nodes"]:
            assert node["evidenceIds"]
            assert node["sourceKind"] in {"observed", "readback"}
        for edge in incident["topology"]["edges"]:
            assert edge["evidenceIds"]
            assert edge["sourceKind"] in {"observed", "readback"}


def test_resolved_disposition_requires_nonempty_passing_readback():
    with pytest.raises(ValidationError, match="passed readback"):
        IncidentDispositionUpdate(status="resolved")

    update = IncidentDispositionUpdate(
        status="resolved",
        updated_at="2026-08-05T15:00:00Z",
        readback={
            "state": "passed",
            "source_kind": "readback",
            "collected_at": "2026-08-05T15:00:00Z",
            "checks": [{"check": "business_ip_present", "passed": True}],
        },
    )
    assert update.readback is not None
    assert update.readback.source_kind == "readback"

    with pytest.raises(ValidationError, match="every check to pass"):
        IncidentDispositionUpdate(
            status="resolved",
            updated_at="2026-08-05T15:00:00Z",
            readback={
                "state": "passed",
                "collected_at": "2026-08-05T15:00:00Z",
                "checks": [{"check": "business_ip_present", "passed": False}],
            },
        )


def test_resolved_disposition_requires_aware_ordered_timestamps():
    payload = {
        "status": "resolved",
        "updated_at": "2026-08-05T14:59:59Z",
        "readback": {
            "state": "passed",
            "collected_at": "2026-08-05T15:00:00Z",
            "checks": [{"check": "business_ip_present", "passed": True}],
        },
    }
    with pytest.raises(ValidationError, match="cannot be earlier"):
        IncidentDispositionUpdate.model_validate(payload)

    payload["updated_at"] = "2026-08-05T15:00:00"
    payload["readback"]["collected_at"] = "2026-08-05T15:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        IncidentDispositionUpdate.model_validate(payload)


def test_disposition_ledger_survives_restart_and_ignores_bad_tail(tmp_path):
    ledger = tmp_path / "incidents" / "disposition.jsonl"
    first = IncidentRepository(ledger_path=ledger)
    updated = first.update_disposition(
        INCIDENT_23,
        IncidentDispositionUpdate(
            status="triaged",
            operator_note="duplicate owner ports are being identified",
        ),
    )
    assert updated["disposition"]["updated_at"].endswith("Z")
    assert ledger.read_text(encoding="utf-8").count("\n") == 1

    restarted = IncidentRepository(ledger_path=ledger)
    assert restarted.get(INCIDENT_23)["disposition"]["status"] == "triaged"
    assert restarted.get(INCIDENT_23)["disposition"]["operator_note"] == (
        "duplicate owner ports are being identified"
    )

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"incident_id":')
    degraded = IncidentRepository(ledger_path=ledger)
    assert degraded.get(INCIDENT_23)["disposition"]["status"] == "triaged"
    assert degraded.ledger_errors and degraded.ledger_errors[-1].startswith("line 2:")

    degraded.update_disposition(
        INCIDENT_23,
        IncidentDispositionUpdate(status="mitigating", operator_note="port isolation pending"),
    )
    recovered = IncidentRepository(ledger_path=ledger)
    assert recovered.get(INCIDENT_23)["disposition"]["status"] == "mitigating"
    assert recovered.ledger_errors and recovered.ledger_errors[-1].startswith("line 2:")

def test_incident_replay_reuses_fact_schema_and_never_claims_live_observation():
    repository = IncidentRepository()
    for incident_id in (INCIDENT_23, INCIDENT_27):
        replay = build_incident_replay(repository, incident_id)
        assert replay["source_kind"] == "simulated"
        assert replay["current_online_observation"] is False
        assert replay["detection"]["detected"] is True
        for event in replay["events"]:
            assert set(_SCHEMA_FIELDS).issubset(event)
            assert event["replay"] is True
            assert event["source_kind"] == "simulated"
            assert event["current_online_observation"] is False
            assert event["incident_id"] == incident_id


def test_shared_replay_transport_rejects_observed_event_before_kafka(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Kafka must not open for non-simulated input")

    monkeypatch.setattr("core.evolve.replay_stream._kafka_types", forbidden)
    result = produce_tagged_replay(
        [{"replay": True, "source_kind": "observed", "case_id": "case"}],
        ["case"],
    )
    assert result["ok"] is False
    assert result["produced"] == 0
    assert "simulated" in result["note"]


def test_gateway_incident_list_detail_and_local_replay(monkeypatch):
    pytest.importorskip("fastapi", reason="gateway extra is not installed")
    from frontend.gateway.app import main as gateway

    repository = IncidentRepository()
    monkeypatch.setattr(gateway, "_incident_repository", repository)

    listing = asyncio.run(gateway.rca_incidents(asset_ip="192.168.1.23"))
    detail = asyncio.run(gateway.rca_incident_detail(INCIDENT_23))
    replay = asyncio.run(gateway.rca_incident_replay(INCIDENT_27, inject=0))

    assert listing["live"] is False
    assert listing["currentOnlineObservation"] is False
    assert [item["id"] for item in listing["incidents"]] == [INCIDENT_23]
    assert detail["incident"]["detection"]["detected"] is True
    assert replay["streamed"] is None
    assert replay["topicStatus"] is None
    assert replay["source_kind"] == "simulated"

    observed_detection = asyncio.run(
        gateway.rca_detect_dual_mac(
            gateway.DualMacDetectionRequest(
                events=[
                    {
                        "event_id": "arp-1",
                        "timestamp": "2026-08-05T15:03:34Z",
                        "ip": "192.168.1.23",
                        "mac": "D4:43:0E:1A:C5:88",
                        "source_kind": "observed",
                    },
                    {
                        "event_id": "arp-2",
                        "timestamp": "2026-08-05T15:03:35Z",
                        "ip": "192.168.1.23",
                        "mac": "50:9A:4C:87:29:B3",
                        "source_kind": "observed",
                    },
                ]
            )
        )
    )
    preflight_detection = asyncio.run(
        gateway.rca_detect_network_preflight(
            gateway.NetworkPreflightDetectionRequest(
                expected_business_ips=["192.168.1.27"],
                cloud_init_enabled=True,
                cloud_init_network_disabled=False,
                persistent_business_ips=[],
                actual_business_ips=None,
                source_kind="simulated",
            )
        )
    )
    assert observed_detection["detection"]["observation_scope"] == "collected_evidence"
    assert observed_detection["currentOnlineObservation"] is False
    assert observed_detection["actionPlan"]["approvalRequired"] is True
    assert preflight_detection["detection"]["preflight"]["detected"] is True
    assert preflight_detection["detection"]["drift"] is None
    assert preflight_detection["readonly"] is True

    openapi = gateway.app.openapi()
    for path, method in (
        ("/api/rca/incidents", "get"),
        ("/api/rca/incidents/{incident_id}", "get"),
        ("/api/rca/incidents/{incident_id}/disposition", "patch"),
        ("/api/rca/incidents/{incident_id}/replay", "post"),
    ):
        schema = openapi["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert "$ref" in schema
    assert "/api/rca/incidents/detect/dual-mac" in openapi["paths"]
    assert "/api/rca/incidents/detect/network-preflight" in openapi["paths"]
