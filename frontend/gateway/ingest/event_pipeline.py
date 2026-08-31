"""Consume live events and persist Autopoiesis incident triggers.

One accepted source event can produce zero or more deterministic alerts.  An
alert is acknowledged only after it has reached the alert topic, ClickHouse,
and the bounded file feed used by the console.  Stable alert identifiers make a
consumer retry safe for the case repository and the replacing table.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.network_rca.event_detection import DetectionPolicy, EventDetector, EventQualityGate

LOGGER = logging.getLogger("autopoiesis-event-pipeline")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class PipelineConfig:
    kafka_brokers: str
    raw_topic: str
    alert_topic: str
    dead_letter_topic: str
    group_id: str
    clickhouse_url: str
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_database: str
    alert_directory: Path
    status_file: Path
    offset_reset: str
    detection_policy: DetectionPolicy

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        database = os.getenv("AUTOPOIESIS_CLICKHOUSE_DATABASE", "autopoiesis")
        if not _IDENTIFIER.fullmatch(database):
            raise ValueError("AUTOPOIESIS_CLICKHOUSE_DATABASE must be a SQL identifier")
        offset_reset = os.getenv("AUTOPOIESIS_OFFSET_RESET", "earliest").casefold()
        if offset_reset != "earliest":
            raise ValueError("Redpanda Pandaproxy consumers require AUTOPOIESIS_OFFSET_RESET=earliest")
        return cls(
            kafka_brokers=os.getenv(
                "AUTOPOIESIS_KAFKA_BROKERS",
                "autopoiesis-redpanda:9093",
            ),
            raw_topic=os.getenv("AUTOPOIESIS_RAW_TOPIC", "autopoiesis.events.raw.v1"),
            alert_topic=os.getenv("AUTOPOIESIS_ALERT_TOPIC", "autopoiesis.alerts.v1"),
            dead_letter_topic=os.getenv("AUTOPOIESIS_DLQ_TOPIC", "autopoiesis.dlq.v1"),
            group_id=os.getenv("AUTOPOIESIS_EVENT_GROUP", "autopoiesis-event-detector-v1"),
            clickhouse_url=os.getenv(
                "AUTOPOIESIS_CLICKHOUSE_URL",
                "http://autopoiesis-clickhouse:8123",
            ).rstrip("/"),
            clickhouse_user=os.getenv("AUTOPOIESIS_CLICKHOUSE_USER", "default"),
            clickhouse_password=os.getenv("AUTOPOIESIS_CLICKHOUSE_PASSWORD", ""),
            clickhouse_database=database,
            alert_directory=Path(
                os.getenv("AUTOPOIESIS_ALERT_DIRECTORY", "/data/autopoiesis-production/stream/alerts")
            ),
            status_file=Path(
                os.getenv(
                    "AUTOPOIESIS_EVENT_STATUS_FILE",
                    "/data/autopoiesis-production/status/event-pipeline.json",
                )
            ),
            offset_reset=offset_reset,
            detection_policy=DetectionPolicy(
                deny_window_seconds=int(os.getenv("AUTOPOIESIS_DENY_WINDOW_SECONDS", "60")),
                deny_threshold=int(os.getenv("AUTOPOIESIS_DENY_THRESHOLD", "30")),
                byte_window_seconds=int(os.getenv("AUTOPOIESIS_BYTE_WINDOW_SECONDS", "300")),
                byte_threshold=int(os.getenv("AUTOPOIESIS_BYTE_THRESHOLD", "20000000")),
                cooldown_seconds=int(os.getenv("AUTOPOIESIS_ALERT_COOLDOWN_SECONDS", "60")),
            ),
        )


class ClickHouseAlertStore:
    def __init__(self, config: PipelineConfig):
        self.config = config

    def _request(self, query: str, payload: bytes = b"") -> bytes:
        from urllib.parse import urlencode

        separator = "&" if "?" in self.config.clickhouse_url else "?"
        url = self.config.clickhouse_url + separator + urlencode({"query": query})
        headers = {"Content-Type": "application/octet-stream"}
        credentials = f"{self.config.clickhouse_user}:{self.config.clickhouse_password}"
        headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def ensure_schema(self) -> None:
        database = self.config.clickhouse_database
        self._request(f"CREATE DATABASE IF NOT EXISTS {database}")
        self._request(
            f"""CREATE TABLE IF NOT EXISTS {database}.alerts (
                alert_id String,
                alert_ts DateTime64(3, 'UTC'),
                rule_id LowCardinality(String),
                severity LowCardinality(String),
                source_event_id String,
                service LowCardinality(String),
                src_device_key String,
                srcip String,
                dstip String,
                alert_json String,
                ingest_ts DateTime64(3, 'UTC') DEFAULT now64(3)
            ) ENGINE = ReplacingMergeTree(ingest_ts)
            PARTITION BY toYYYYMM(alert_ts)
            ORDER BY alert_id"""
        )

    def insert(self, alert: dict[str, Any]) -> None:
        excerpt = alert.get("event_excerpt") or {}
        row = {
            "alert_id": str(alert.get("alert_id") or ""),
            "alert_ts": str(alert.get("alert_ts") or "").replace("T", " ").replace("+00:00", ""),
            "rule_id": str(alert.get("rule_id") or "unknown"),
            "severity": str(alert.get("severity") or "unknown"),
            "source_event_id": str(alert.get("source_event_id") or ""),
            "service": str(excerpt.get("service") or "unknown"),
            "src_device_key": str(alert.get("src_device_key") or ""),
            "srcip": str(excerpt.get("srcip") or ""),
            "dstip": str(excerpt.get("dstip") or ""),
            "alert_json": json.dumps(alert, ensure_ascii=False, separators=(",", ":")),
            "ingest_ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        }
        query = f"INSERT INTO {self.config.clickhouse_database}.alerts FORMAT JSONEachRow"
        self._request(query, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))


class AlertFileFeed:
    def __init__(self, directory: Path):
        self.directory = directory

    def append(self, alert: dict[str, Any]) -> Path:
        observed_at = str(alert.get("alert_ts") or "").replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(observed_at)
        except ValueError:
            stamp = datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        alert_id = str(alert.get("alert_id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", alert_id):
            raise ValueError("alert_id must be a SHA-256 hex digest")
        path = self.directory / (
            f"alerts-{stamp.astimezone(timezone.utc).strftime('%Y%m%d-%H')}-{alert_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(alert, ensure_ascii=False, separators=(",", ":")) + "\n"
        if path.exists():
            return path
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return path


@dataclass(frozen=True)
class ProxyMessage:
    topic: str
    partition: int
    offset: int
    value: dict[str, Any]


class KafkaClient:
    """Native Kafka adapter with synchronous commits after all sinks succeed."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        consumer_factory: Any | None = None,
        producer_factory: Any | None = None,
        topic_partition_factory: Any | None = None,
    ):
        self.config = config
        if consumer_factory is None or producer_factory is None or topic_partition_factory is None:
            try:
                from confluent_kafka import Consumer, Producer, TopicPartition
            except ImportError as error:  # pragma: no cover - container dependency
                raise RuntimeError("event pipeline requires confluent-kafka") from error
            consumer_factory = consumer_factory or Consumer
            producer_factory = producer_factory or Producer
            topic_partition_factory = topic_partition_factory or TopicPartition
        self._consumer_factory = consumer_factory
        self._producer_factory = producer_factory
        self._topic_partition_factory = topic_partition_factory
        self.consumer: Any | None = None
        self.producer: Any | None = None

    def open(self) -> None:
        self.consumer = self._consumer_factory({
            "bootstrap.servers": self.config.kafka_brokers,
            "group.id": self.config.group_id,
            "auto.offset.reset": self.config.offset_reset,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "partition.assignment.strategy": "cooperative-sticky",
        })
        self.producer = self._producer_factory({
            "bootstrap.servers": self.config.kafka_brokers,
            "enable.idempotence": True,
            "acks": "all",
        })
        self.consumer.subscribe([self.config.raw_topic])

    def poll(self) -> list[ProxyMessage]:
        if self.consumer is None:
            raise RuntimeError("Kafka consumer is not open")
        rows = self.consumer.consume(num_messages=1000, timeout=1.0)
        messages: list[ProxyMessage] = []
        for row in rows or ():
            error = row.error()
            if error is not None:
                raise RuntimeError(f"Kafka consume failed: {error}")
            value = row.value()
            if isinstance(value, bytes):
                value = json.loads(value.decode("utf-8"))
            elif isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                raise ValueError("event value must be a JSON object")
            messages.append(ProxyMessage(
                topic=str(row.topic() or self.config.raw_topic),
                partition=int(row.partition()),
                offset=int(row.offset()),
                value=value,
            ))
        return messages

    def publish(self, topic: str, key: str, value: dict[str, Any]) -> None:
        if self.producer is None:
            raise RuntimeError("Kafka producer is not open")
        failures: list[str] = []

        def delivered(error: Any, _message: Any) -> None:
            if error is not None:
                failures.append(str(error))

        self.producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            on_delivery=delivered,
        )
        remaining = self.producer.flush(10)
        if remaining or failures:
            raise RuntimeError("Kafka did not acknowledge the alert: " + "; ".join(failures))

    def commit(self, messages: list[ProxyMessage]) -> None:
        if messages and self.consumer is not None:
            next_offsets: dict[tuple[str, int], int] = {}
            for message in messages:
                key = (message.topic, message.partition)
                next_offsets[key] = max(next_offsets.get(key, 0), message.offset + 1)
            partitions = [
                self._topic_partition_factory(topic, partition, offset)
                for (topic, partition), offset in sorted(next_offsets.items())
            ]
            self.consumer.commit(offsets=partitions, asynchronous=False)

    def close(self) -> None:
        if self.consumer is not None:
            self.consumer.close()
            self.consumer = None
        if self.producer is not None:
            self.producer.flush(10)
            self.producer = None


def _write_status(path: Path, stats: dict[str, Any], error: str | None = None) -> None:
    state = {
        **stats,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run(config: PipelineConfig | None = None) -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = config or PipelineConfig.from_env()
    store = ClickHouseAlertStore(config)
    store.ensure_schema()
    feed = AlertFileFeed(config.alert_directory)
    detector = EventDetector(config.detection_policy)
    gate = EventQualityGate(config.detection_policy.accepted_source_kinds)
    proxy = KafkaClient(config)
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    stats: dict[str, Any] = {
        "events_seen": 0,
        "events_accepted": 0,
        "alerts_persisted": 0,
        "events_rejected": {},
        "source_topic": config.raw_topic,
        "alert_topic": config.alert_topic,
        "clickhouse_database": config.clickhouse_database,
    }
    _write_status(config.status_file, stats)
    LOGGER.info("event pipeline started raw_topic=%s alert_topic=%s", config.raw_topic, config.alert_topic)
    try:
        while not stop_requested:
            try:
                if proxy.consumer is None:
                    proxy.open()
                messages = proxy.poll()
                if not messages:
                    continue
                for message in messages:
                    stats["events_seen"] += 1
                    event = message.value
                    stats["last_event_at"] = event.get("event_ts")
                    accepted, reason = gate.evaluate(event)
                    if not accepted:
                        rejected = stats["events_rejected"]
                        rejected[reason] = rejected.get(reason, 0) + 1
                        continue
                    stats["events_accepted"] += 1
                    for alert in detector.process(event):
                        proxy.publish(config.alert_topic, str(alert["alert_id"]), alert)
                        store.insert(alert)
                        feed.append(alert)
                        stats["alerts_persisted"] += 1
                proxy.commit(messages)
                _write_status(config.status_file, stats)
            except Exception as error:
                LOGGER.exception("event batch failed; reopening from committed offsets")
                _write_status(config.status_file, stats, f"{type(error).__name__}: {error}")
                proxy.close()
                time.sleep(2)
    finally:
        proxy.close()
        _write_status(config.status_file, stats)


if __name__ == "__main__":
    run()
