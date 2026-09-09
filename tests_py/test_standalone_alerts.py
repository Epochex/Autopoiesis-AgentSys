import json

import pytest

from domains.network_rca.event_detection import DetectionPolicy
from frontend.gateway.ingest.standalone_alerts import (
    AlertFileFeed,
    AlertStoreConfig,
    StandaloneAlertPipeline,
)


def _config(tmp_path, **policy):
    return AlertStoreConfig(
        clickhouse_url="http://clickhouse.invalid:8123",
        clickhouse_user="test",
        clickhouse_password="test",
        clickhouse_database="autopoiesis",
        alert_directory=tmp_path,
        detection_policy=DetectionPolicy(**policy),
    )


def _event(index: int) -> dict:
    return {
        "event_id": f"event-{index}",
        "event_ts": f"2026-08-31T12:00:{index:02d}+00:00",
        "parse_status": "ok",
        "source_kind": "real",
        "type": "traffic",
        "subtype": "forward",
        "action": "deny",
        "srcip": "192.0.2.10",
        "dstip": "198.51.100.20",
        "dstport": 443,
        "service": "HTTPS",
        "policyid": 7,
    }


def test_alert_file_feed_writes_one_idempotent_file(tmp_path):
    alert = {
        "alert_id": "a" * 64,
        "alert_ts": "2026-08-31T12:03:04+00:00",
        "rule_id": "deny_burst_v2",
    }
    path = AlertFileFeed(tmp_path).append(alert)
    assert path.name == f"alerts-20260831-12-{'a' * 64}.jsonl"
    assert json.loads(path.read_text().strip()) == alert
    assert AlertFileFeed(tmp_path).append(alert) == path
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


def test_standalone_pipeline_closes_event_to_alert_path(tmp_path):
    stored = []

    class Store:
        def insert(self, alert):
            stored.append(alert)

    pipeline = StandaloneAlertPipeline(
        _config(tmp_path, deny_threshold=2),
        store=Store(),
    )
    result = pipeline.process_batch([_event(0), _event(1)])

    assert result == {
        "events_seen": 2,
        "events_accepted": 2,
        "events_rejected": 0,
        "alerts_persisted": 1,
    }
    assert len(stored) == 1
    assert stored[0]["source_event_id"] == "event-1"
    assert list(tmp_path.glob("*.jsonl"))


def test_failed_alert_sink_rolls_back_detection_window_for_source_retry(tmp_path):
    attempts = 0

    class Store:
        def insert(self, alert):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("alert store unavailable")

    pipeline = StandaloneAlertPipeline(
        _config(tmp_path, deny_threshold=2),
        store=Store(),
    )
    events = [_event(0), _event(1)]

    with pytest.raises(OSError, match="alert store unavailable"):
        pipeline.process_batch(events)
    assert pipeline.status()["detector_events_seen_since_start"] == 0

    result = pipeline.process_batch(events)
    assert result["events_accepted"] == 2
    assert result["alerts_persisted"] == 1
    assert attempts == 2
