"""Acceptance contracts for the two documented network incidents.

These tests deliberately separate three kinds of information:

* manually documented historical facts;
* deterministic detector/replay inputs, which are simulated;
* post-action readback, which must be collected before an incident is resolved.

They do not contact either host and therefore cannot turn fixture data into a
claim about current online state.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from domains.network_rca.incidents import (
    IncidentDispositionUpdate,
    IncidentRepository,
    build_incident_replay,
    detect_dual_mac_window,
    detect_host_network_drift,
    detect_host_network_preflight,
    load_incident_fixture,
)


INCIDENT_23 = "inc-192-168-1-23-dual-mac-arp-drift"
INCIDENT_27 = "inc-192-168-1-27-cloud-init-network-drift"


def _event(
    seconds: int,
    mac: str,
    *,
    event_id: str,
    source_kind: str = "observed",
) -> dict:
    base = datetime(2026, 8, 5, tzinfo=timezone.utc)
    return {
        "event_id": event_id,
        "timestamp": (base + timedelta(seconds=seconds)).isoformat(),
        "ip": "192.168.1.23",
        "mac": mac,
        "source_kind": source_kind,
    }


def test_fixture_contains_only_the_two_bounded_historical_incidents() -> None:
    payload = load_incident_fixture()
    incidents = {item["id"]: item for item in payload["incidents"]}

    assert set(incidents) == {INCIDENT_23, INCIDENT_27}
    assert payload["data_mode"] == "historical_fixture"
    assert payload["current_online_observation"] is False
    assert incidents[INCIDENT_23]["asset"]["ip"] == "192.168.1.23"
    assert incidents[INCIDENT_27]["asset"]["ip"] == "192.168.1.27"
    for incident in incidents.values():
        assert incident["classification"] == "historical_real_incident"
        assert incident["current_online_observation"] is False
    assert incidents[INCIDENT_23]["disposition"] == {
        "status": "awaiting_readback",
        "operator_note": None,
        "updated_at": None,
        "readback": {
            "state": "not_collected",
            "source_kind": None,
            "collected_at": None,
            "checks": [],
        },
    }
    partial = incidents[INCIDENT_27]["disposition"]
    assert partial["status"] == "awaiting_readback"
    assert partial["readback"]["state"] == "partial"
    assert partial["readback"]["source_kind"] == "readback"
    checks = {check["check"]: check for check in partial["readback"]["checks"]}
    assert checks["business_ip_present"]["passed"] is True
    assert checks["default_route_present"]["passed"] is True
    assert checks["ssh_service_active"]["passed"] is True
    assert checks["controlled_restart_persistence"]["passed"] is False
    assert checks["controlled_restart_persistence"]["evidence_id"] is None


def test_fixture_evidence_is_ordered_unique_and_source_attributed() -> None:
    payload = load_incident_fixture()
    evidence_ids: set[str] = set()

    for incident in payload["incidents"]:
        timeline = incident["evidence_timeline"]
        assert [row["sequence"] for row in timeline] == list(
            range(1, len(timeline) + 1)
        )
        timestamps = [
            datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
            for row in timeline
            if row["occurred_at"] is not None
        ]
        assert timestamps == sorted(timestamps)
        for row in timeline:
            assert row["source_kind"] in {"observed", "manual", "readback"}
            assert row["evidence_id"] not in evidence_ids
            evidence_ids.add(row["evidence_id"])
            assert row["summary"].strip()
            assert isinstance(row["facts"], dict) and row["facts"]


def test_dual_mac_detector_has_an_inclusive_five_minute_boundary() -> None:
    mac_a = "02:00:00:00:23:01"
    mac_b = "02:00:00:00:23:02"

    at_boundary = detect_dual_mac_window(
        [
            _event(0, mac_a, event_id="first"),
            _event(300, mac_b, event_id="second"),
        ],
        window_seconds=300,
    )
    outside_boundary = detect_dual_mac_window(
        [
            _event(0, mac_a, event_id="first"),
            _event(301, mac_b, event_id="second"),
        ],
        window_seconds=300,
    )

    assert at_boundary["detected"] is True
    assert at_boundary["findings"] == [
        {
            "ip": "192.168.1.23",
            "macs": [mac_a, mac_b],
            "first_seen": "2026-08-05T00:00:00Z",
            "last_seen": "2026-08-05T00:05:00Z",
            "evidence_ids": ["first", "second"],
            "source_kinds": ["observed"],
        }
    ]
    assert outside_boundary["detected"] is False


def test_dual_mac_detector_sorts_input_and_rejects_same_mac_noise() -> None:
    mac_a = "02:00:00:00:23:01"
    mac_b = "02:00:00:00:23:02"
    result = detect_dual_mac_window(
        [
            _event(80, mac_a.upper(), event_id="return", source_kind="simulated"),
            _event(45, mac_b, event_id="alternate", source_kind="simulated"),
            _event(0, mac_a, event_id="first", source_kind="simulated"),
        ]
    )
    same_mac = detect_dual_mac_window(
        [
            _event(0, mac_a, event_id="first"),
            _event(45, mac_a.upper(), event_id="repeat"),
        ]
    )

    assert result["detected"] is True
    assert result["findings"][0]["evidence_ids"] == [
        "first",
        "alternate",
        "return",
    ]
    assert result["findings"][0]["macs"] == [mac_a, mac_b]
    assert result["observation_scope"] == "replay_simulation"
    assert result["current_online_observation"] is False
    assert same_mac["detected"] is False


def test_fixture_dual_mac_detector_uses_observed_flips_with_evidence_links() -> None:
    incident = next(
        item for item in load_incident_fixture()["incidents"] if item["id"] == INCIDENT_23
    )
    detector = incident["detector"]
    observed_rows = [
        row
        for row in incident["evidence_timeline"]
        if row["source_kind"] == "observed" and row["facts"].get("samples")
    ]

    assert detector["input_source_kind"] == "observed"
    assert detector["time_basis"] == "captured_utc"
    assert detector["window_seconds"] == 300
    assert all(event["source_kind"] == "observed" for event in detector["events"])
    assert observed_rows, "the plotted MAC series must be backed by observed evidence"

    metric_points: list[dict] = []
    for row in observed_rows:
        for sample in row["facts"]["samples"]:
            timestamp, mac = sample[:2]
            metric_points.append(
                {
                    "timestamp": timestamp,
                    "mac": mac.lower(),
                    "evidence_id": row["evidence_id"],
                }
            )

    assert len(metric_points) >= 5
    assert len({point["mac"] for point in metric_points}) >= 2
    assert all(point["evidence_id"] for point in metric_points)
    parsed_times = [
        datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
        for point in metric_points
    ]
    assert parsed_times == sorted(parsed_times)
    assert (parsed_times[-1] - parsed_times[0]).total_seconds() <= detector["window_seconds"]
    flip_count = sum(
        left["mac"] != right["mac"]
        for left, right in zip(metric_points, metric_points[1:])
    )
    assert flip_count >= 2

    detection = IncidentRepository().get(INCIDENT_23)["detection"]
    assert detection["detected"] is True
    assert detection["source_kinds"] == ["observed"]
    assert detection["observation_scope"] == "collected_evidence"
    assert detection["current_online_observation"] is False



def test_every_plotted_metric_point_has_a_valid_evidence_reference() -> None:
    repository = IncidentRepository()

    for incident_id in (INCIDENT_23, INCIDENT_27):
        detail = repository.get(incident_id)
        evidence_ids = {item["id"] for item in detail["evidence"]}
        metric_points = [
            point for metric in detail["metrics"] for point in metric["points"]
        ]
        assert metric_points
        for point in metric_points:
            assert point["evidenceIds"], (
                f"every plotted point for {incident_id} needs a drill-down reference"
            )
            assert set(point["evidenceIds"]).issubset(evidence_ids)


def test_host_network_preflight_requires_persistent_ip_and_network_guard() -> None:
    safe = detect_host_network_preflight(
        expected_business_ips=["192.168.1.27"],
        cloud_init_enabled=True,
        cloud_init_network_disabled=True,
        persistent_business_ips=["192.168.1.27"],
        source_kind="observed",
    )
    unsafe = detect_host_network_preflight(
        expected_business_ips=["192.168.1.27"],
        cloud_init_enabled=True,
        cloud_init_network_disabled=False,
        persistent_business_ips=[],
        source_kind="simulated",
    )

    assert safe["detected"] is False
    assert safe["risks"] == []
    assert safe["observation_scope"] == "collected_evidence"
    assert unsafe["detected"] is True
    assert unsafe["risks"] == [
        "cloud_init_can_manage_network_on_restart",
        "expected_business_ip_absent_from_persistent_config",
    ]
    assert unsafe["missing_from_persistent_config"] == ["192.168.1.27"]
    assert unsafe["observation_scope"] == "replay_simulation"
    assert unsafe["current_online_observation"] is False


def test_host_network_readback_reports_missing_and_unexpected_addresses() -> None:
    result = detect_host_network_drift(
        expected_business_ips=["192.168.1.27"],
        actual_business_ips=["192.168.1.99"],
        source_kind="readback",
    )

    assert result["detected"] is True
    assert result["missing_business_ips"] == ["192.168.1.27"]
    assert result["unexpected_business_ips"] == ["192.168.1.99"]
    assert result["observation_scope"] == "collected_evidence"
    assert result["current_online_observation"] is False


def test_resolved_status_requires_passing_readback() -> None:
    with pytest.raises(ValidationError, match="requires a passed readback"):
        IncidentDispositionUpdate(status="resolved")
    with pytest.raises(ValidationError):
        IncidentDispositionUpdate(
            status="resolved",
            readback={
                "state": "failed",
                "collected_at": "2026-08-05T13:20:00Z",
                "checks": [{"check": "business_ip", "passed": False}],
            },
        )
    with pytest.raises(ValidationError, match="every check to pass"):
        IncidentDispositionUpdate(
            status="resolved",
            updated_at="2026-08-05T13:20:00Z",
            readback={
                "state": "passed",
                "collected_at": "2026-08-05T13:20:00Z",
                "checks": [{"check": "business_ip", "passed": False}],
            },
        )

    accepted = IncidentDispositionUpdate(
        status="resolved",
        operator_note="static address and restart persistence verified",
        updated_at="2026-08-05T13:20:00Z",
        readback={
            "state": "passed",
            "collected_at": "2026-08-05T13:20:00Z",
            "checks": [{"check": "business_ip", "passed": True}],
        },
    )
    assert accepted.readback is not None
    assert accepted.readback.source_kind == "readback"


def test_repository_persists_disposition_readback_within_the_process() -> None:
    repository = IncidentRepository()
    update = IncidentDispositionUpdate(
        status="resolved",
        operator_note="contract-test readback",
        updated_at="2026-08-05T13:20:00Z",
        readback={
            "state": "passed",
            "collected_at": "2026-08-05T13:20:00Z",
            "checks": [{"check": "business_ip", "passed": True}],
        },
    )

    updated = repository.update_disposition(INCIDENT_27, update)

    assert updated["disposition"]["status"] == "resolved"
    assert updated["disposition"]["readback"]["state"] == "passed"
    assert updated["disposition"]["readback"]["source_kind"] == "readback"
    assert [row["id"] for row in repository.list(status="resolved")] == [INCIDENT_27]


def _assert_no_current_online_claim(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"current_online_observation", "currentOnlineObservation"}:
                assert nested is False
            _assert_no_current_online_claim(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_current_online_claim(nested)


def test_gateway_http_contract_and_openapi_routes(monkeypatch) -> None:
    pytest.importorskip("fastapi", reason="gateway extra is not installed")
    from fastapi import HTTPException
    from frontend.gateway.app import main as gateway

    repository = IncidentRepository()
    monkeypatch.setattr(gateway, "_incident_repository", repository)

    listing = asyncio.run(gateway.rca_incidents())
    detail = asyncio.run(gateway.rca_incident_detail(INCIDENT_27))
    replay = asyncio.run(gateway.rca_incident_replay(INCIDENT_23, inject=0))
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(gateway.rca_incident_detail("does-not-exist"))
    assert unknown.value.status_code == 404

    # Apply the same response models that FastAPI applies on an HTTP response.
    gateway.IncidentListResponse.model_validate(listing)
    gateway.IncidentDetailResponse.model_validate(detail)
    gateway.IncidentReplayResponse.model_validate(replay)

    assert listing["ok"] is True
    assert listing["live"] is False
    assert listing["count"] == len(listing["incidents"]) == 2
    assert listing["dataMode"] == "historical_fixture"
    assert listing["datasetKind"] == "historical-real-incidents"

    assert detail["live"] is False
    assert detail["incident"]["id"] == INCIDENT_27
    assert detail["incident"]["detection"]["source_kinds"] == ["simulated"]
    assert detail["incident"]["disposition"]["readback"]["source_kind"] == "readback"

    assert replay["live"] is False
    assert replay["streamed"] is None
    assert replay["topicStatus"] is None
    assert replay["source_kind"] == "simulated"
    for response in (listing, detail, replay):
        _assert_no_current_online_claim(response)

    paths = gateway.app.openapi()["paths"]
    assert set(paths["/api/rca/incidents"]) == {"get"}
    assert set(paths["/api/rca/incidents/{incident_id}"]) == {"get"}
    assert set(paths["/api/rca/incidents/{incident_id}/disposition"]) == {"patch"}
    assert set(paths["/api/rca/incidents/{incident_id}/replay"]) == {"post"}
    patch_schema = paths["/api/rca/incidents/{incident_id}/disposition"]["patch"]
    request_schema = patch_schema["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/IncidentDispositionUpdate")
    assert patch_schema["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/IncidentDispositionAPIResponse")


@pytest.mark.parametrize(
    ("incident_id", "expected_detection_scope"),
    [
        (INCIDENT_23, "replay_simulation"),
        (INCIDENT_27, "replay_simulation"),
    ],
)
def test_incident_replay_is_strictly_simulated_and_time_ordered(
    incident_id: str, expected_detection_scope: str,
) -> None:
    replay = build_incident_replay(IncidentRepository(), incident_id)
    events = replay["events"]

    assert replay["replay"] is True
    assert replay["source_kind"] == "simulated"
    assert replay["time_basis"] == "simulated_relative_clock"
    assert replay["current_online_observation"] is False
    assert events
    assert [event["relative_seconds"] for event in events] == sorted(
        event["relative_seconds"] for event in events
    )
    assert len({event["event_id"] for event in events}) == len(events)
    for event in events:
        assert event["incident_id"] == incident_id
        assert event["replay"] is True
        assert event["source_kind"] == "simulated"
        assert event["current_online_observation"] is False
        assert event["fault_context"]["source_kind"] == "simulated"
    assert replay["detection"]["current_online_observation"] is False
    assert replay["detection"]["observation_scope"] == expected_detection_scope
