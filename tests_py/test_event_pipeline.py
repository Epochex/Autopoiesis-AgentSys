import json
from types import SimpleNamespace

from frontend.gateway.ingest.event_pipeline import AlertFileFeed, KafkaClient, ProxyMessage


def test_alert_file_feed_writes_hourly_jsonl(tmp_path):
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


def test_redpanda_commit_persists_next_offset_for_each_partition():
    class Consumer:
        def __init__(self):
            self.calls = []

        def commit(self, **kwargs):
            self.calls.append(kwargs)

    consumer = Consumer()
    client = KafkaClient(
        SimpleNamespace(),
        consumer_factory=lambda _config: consumer,
        producer_factory=lambda _config: object(),
        topic_partition_factory=lambda topic, partition, offset: (topic, partition, offset),
    )
    client.consumer = consumer

    client.commit([
        ProxyMessage("events", 1, 7, {"event_id": "event-1"}),
        ProxyMessage("events", 0, 9, {"event_id": "event-2"}),
        ProxyMessage("events", 1, 11, {"event_id": "event-3"}),
    ])

    assert consumer.calls == [{
        "offsets": [("events", 0, 10), ("events", 1, 12)],
        "asynchronous": False,
    }]
