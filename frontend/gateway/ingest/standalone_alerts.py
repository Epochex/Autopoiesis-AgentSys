"""In-process alert detection and persistence for the standalone deployment.

The collector owns source progress, historical archival, detection windows and
alert persistence in one process.  A source checkpoint advances only after all
three sinks succeed.  Stable event and alert identifiers make a retried batch
safe for the replacing ClickHouse tables and the atomic file feed.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from domains.network_rca.event_detection import DetectionPolicy, EventDetector, EventQualityGate

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AlertStoreConfig:
    clickhouse_url: str
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_database: str
    alert_directory: Path
    detection_policy: DetectionPolicy

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.clickhouse_database):
            raise ValueError("clickhouse_database must be a SQL identifier")


class ClickHouseAlertStore:
    def __init__(self, config: AlertStoreConfig):
        self.config = config

    def _request(self, query: str, payload: bytes = b"") -> bytes:
        from urllib.parse import urlencode

        separator = "&" if "?" in self.config.clickhouse_url else "?"
        url = self.config.clickhouse_url.rstrip("/") + separator + urlencode({"query": query})
        credentials = f"{self.config.clickhouse_user}:{self.config.clickhouse_password}"
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
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
            "alert_ts": str(alert.get("alert_ts") or "").replace("T", " ").replace(
                "+00:00", ""
            ),
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
        if path.exists():
            return path
        payload = json.dumps(alert, ensure_ascii=False, separators=(",", ":")) + "\n"
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


class StandaloneAlertPipeline:
    """Evaluate one collector batch and land every resulting alert locally."""

    def __init__(
        self,
        config: AlertStoreConfig,
        *,
        store: Any | None = None,
        feed: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store or ClickHouseAlertStore(config)
        self.feed = feed or AlertFileFeed(config.alert_directory)
        self.detector = EventDetector(config.detection_policy)
        self.gate = EventQualityGate(config.detection_policy.accepted_source_kinds)
        self.events_seen = 0
        self.events_accepted = 0
        self.alerts_persisted = 0
        self.events_rejected: dict[str, int] = {}
        self.last_event_at: str | None = None

    def ensure_schema(self) -> None:
        self.store.ensure_schema()

    def warmup(self, events: Iterable[dict[str, Any]]) -> int:
        """Prime detection windows from source records before the committed offset."""
        accepted = 0
        for event in events:
            allowed, _reason = self.gate.evaluate(event)
            if not allowed:
                continue
            accepted += 1
            self.detector.process(event)
        return accepted

    def process_batch(self, events: Iterable[dict[str, Any]]) -> dict[str, int]:
        rows = list(events)
        detector_before = copy.deepcopy(self.detector)
        accepted_ids: list[str] = []
        accepted = 0
        rejected = 0
        persisted = 0
        rejected_by_reason: dict[str, int] = {}
        last_event_at = self.last_event_at
        try:
            for event in rows:
                last_event_at = str(event.get("event_ts") or "") or last_event_at
                allowed, reason = self.gate.evaluate(event)
                if not allowed:
                    rejected += 1
                    rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
                    continue
                accepted += 1
                accepted_ids.append(str(event["event_id"]))
                for alert in self.detector.process(event):
                    self.store.insert(alert)
                    self.feed.append(alert)
                    persisted += 1
        except Exception:
            self.detector = detector_before
            # These ids came from a source batch whose checkpoint has not moved.
            # Removing them lets the next attempt reproduce the same alerts.
            self.gate.forget(accepted_ids)
            raise
        self.events_seen += len(rows)
        self.events_accepted += accepted
        self.alerts_persisted += persisted
        self.last_event_at = last_event_at
        for reason, count in rejected_by_reason.items():
            self.events_rejected[reason] = self.events_rejected.get(reason, 0) + count
        return {
            "events_seen": len(rows),
            "events_accepted": accepted,
            "events_rejected": rejected,
            "alerts_persisted": persisted,
        }

    def status(self) -> dict[str, Any]:
        return {
            "detector_events_seen_since_start": self.events_seen,
            "detector_events_accepted_since_start": self.events_accepted,
            "detector_events_rejected_since_start": sum(self.events_rejected.values()),
            "detector_rejected_by_reason": dict(sorted(self.events_rejected.items())),
            "alerts_persisted_since_start": self.alerts_persisted,
            "detector_last_event_at": self.last_event_at,
        }
