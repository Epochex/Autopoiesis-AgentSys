from __future__ import annotations

import json

from core.evolve import replay_stream


class _Response:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_replay_uses_http_proxy_from_host(monkeypatch):
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        payload = json.loads(request.data)
        captured["payload"] = payload
        return _Response(json.dumps({
            "offsets": [
                {"partition": 0, "offset": index, "error_code": 0}
                for index, _record in enumerate(payload["records"])
            ]
        }))

    monkeypatch.setattr(replay_stream, "_REDPANDA_PROXY_URL", "http://redpanda-proxy:8082")
    monkeypatch.setattr(replay_stream, "_KAFKA_BROKERS", "")
    monkeypatch.setattr(replay_stream.urllib.request, "urlopen", fake_urlopen)

    event = {
        "replay": True,
        "source_kind": "simulated",
        "case_id": "case-1",
        "event_ts": "2026-08-31T00:00:00.000Z",
        "type": "event",
        "subtype": "test",
    }
    result = replay_stream.produce_tagged_replay([event], ["case-1"])

    assert result["ok"] is True and result["produced"] == 1
    assert captured["url"] == "http://redpanda-proxy:8082/topics/autopoiesis.events.replay.v1"
    record = captured["payload"]["records"][0]
    assert record["key"] == record["value"]["event_id"]


def test_topic_status_uses_admin_metric(monkeypatch):
    metrics = "\n".join([
        '# TYPE redpanda_kafka_max_offset gauge',
        'redpanda_kafka_max_offset{redpanda_partition="0",redpanda_topic="autopoiesis.events.replay.v1"} 4.000000',
        'redpanda_kafka_max_offset{redpanda_partition="1",redpanda_topic="other"} 99.000000',
    ])

    monkeypatch.setattr(replay_stream, "_REDPANDA_ADMIN_URL", "http://redpanda-admin:9644")
    monkeypatch.setattr(
        replay_stream.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(metrics),
    )

    assert replay_stream.topic_status() == {"events": 4, "degraded": False}


def test_replay_transport_degrades_when_no_endpoint_is_configured(monkeypatch):
    monkeypatch.setattr(replay_stream, "_REDPANDA_PROXY_URL", "")
    monkeypatch.setattr(replay_stream, "_REDPANDA_ADMIN_URL", "")
    monkeypatch.setattr(replay_stream, "_KAFKA_BROKERS", "")

    event = {"replay": True, "source_kind": "simulated", "case_id": "case-1"}
    assert replay_stream.produce_tagged_replay([event], ["case-1"])["degraded"] is True
    assert replay_stream.topic_status()["degraded"] is True
