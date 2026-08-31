"""Deterministic event detection for the live Autopoiesis data plane.

The collector emits normalized FortiGate events.  This module turns a small,
explicit set of known conditions into incident triggers.  Open-ended diagnosis
starts after the trigger has been persisted; benchmark labels and replay-only
fields are intentionally outside this production detector.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class DetectionPolicy:
    deny_window_seconds: int = 60
    deny_threshold: int = 30
    byte_window_seconds: int = 300
    byte_threshold: int = 20_000_000
    cooldown_seconds: int = 60
    accepted_source_kinds: tuple[str, ...] = ("real",)


class EventQualityGate:
    """Reject malformed, duplicated, and non-production stream records."""

    def __init__(self, accepted_source_kinds: tuple[str, ...] = ("real",), capacity: int = 200_000):
        self.accepted_source_kinds = frozenset(accepted_source_kinds)
        self.capacity = max(10_000, capacity)
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def evaluate(self, event: dict[str, Any]) -> tuple[bool, str]:
        if str(event.get("parse_status") or "ok").casefold() != "ok":
            return False, "parse_status_not_ok"
        if str(event.get("source_kind") or "real").casefold() not in self.accepted_source_kinds:
            return False, "source_kind_not_allowed"
        for key in ("event_id", "event_ts", "type", "subtype"):
            if event.get(key) in (None, ""):
                return False, f"missing_{key}"
        event_id = str(event["event_id"])
        if event_id in self._seen:
            return False, "duplicate_event_id"
        self._seen.add(event_id)
        self._order.append(event_id)
        while len(self._order) > self.capacity:
            self._seen.discard(self._order.popleft())
        return True, "accepted"


class EventDetector:
    """Maintain bounded event-time windows and emit stable alert envelopes."""

    def __init__(self, policy: DetectionPolicy | None = None):
        self.policy = policy or DetectionPolicy()
        self._deny: dict[tuple[str, ...], deque[datetime]] = defaultdict(deque)
        self._byte: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)
        self._last_alert: dict[str, datetime] = {}

    def process(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        observed_at = _event_time(event)
        if observed_at is None:
            return []
        alerts: list[dict[str, Any]] = []
        deny = self._deny_burst(event, observed_at)
        if deny is not None:
            alerts.append(deny)
        byte = self._byte_spike(event, observed_at)
        if byte is not None:
            alerts.append(byte)
        return alerts

    def _deny_burst(self, event: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if str(event.get("action") or "").casefold() != "deny":
            return None
        key = tuple(str(event.get(field) or "") for field in (
            "srcip", "dstip", "dstport", "service", "policyid", "subtype",
        ))
        bucket = self._deny[key]
        cutoff = now - timedelta(seconds=self.policy.deny_window_seconds)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) < self.policy.deny_threshold:
            return None
        alert_key = "deny_burst|" + "|".join(key)
        if not self._cooldown(alert_key, now):
            return None
        return _alert(
            "deny_burst_v2",
            "warning",
            event,
            now,
            {"flow_key": key},
            {
                "deny_count": len(bucket),
                "window_sec": self.policy.deny_window_seconds,
                "threshold": self.policy.deny_threshold,
            },
        )

    def _byte_spike(self, event: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        source = str(event.get("srcip") or "")
        if not source:
            return None
        try:
            byte_count = int(event.get("bytes_total") or 0)
        except (TypeError, ValueError):
            byte_count = 0
        if byte_count <= 0:
            return None
        bucket = self._byte[source]
        cutoff = now - timedelta(seconds=self.policy.byte_window_seconds)
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        bucket.append((now, byte_count))
        total = sum(item[1] for item in bucket)
        if total < self.policy.byte_threshold or not self._cooldown(f"byte_spike|{source}", now):
            return None
        return _alert(
            "byte_spike_v2",
            "critical",
            event,
            now,
            {"srcip": source},
            {
                "bytes_sum": total,
                "window_sec": self.policy.byte_window_seconds,
                "threshold": self.policy.byte_threshold,
            },
        )

    def _cooldown(self, key: str, now: datetime) -> bool:
        previous = self._last_alert.get(key)
        if previous is not None and (now - previous).total_seconds() < self.policy.cooldown_seconds:
            return False
        self._last_alert[key] = now
        return True


def _event_time(event: dict[str, Any]) -> datetime | None:
    value = str(event.get("event_ts") or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _alert(
    rule_id: str,
    severity: str,
    event: dict[str, Any],
    observed_at: datetime,
    dimensions: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    source_event_id = str(event.get("event_id") or "")
    identity = f"{rule_id}|{source_event_id}|{observed_at.isoformat()}"
    alert_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    excerpt_fields = (
        "event_id", "event_ts", "type", "subtype", "action", "policyid", "policytype",
        "sessionid", "proto", "srcip", "srcport", "srcintf", "srcintfrole", "dstip",
        "dstport", "dstintf", "dstintfrole", "service", "src_device_key", "srcmac",
        "mastersrcmac", "devname", "srcname", "devtype", "srchwvendor", "srcfamily",
        "srchwversion", "appcat", "bytes_total", "pkts_total",
    )
    excerpt = {field: event.get(field) for field in excerpt_fields}
    source = event.get("source")
    if isinstance(source, dict):
        excerpt["source_path"] = source.get("path", "")
    return {
        "schema_version": 2,
        "alert_id": alert_id,
        "alert_ts": observed_at.isoformat(),
        "rule_id": rule_id,
        "severity": severity,
        "src_device_key": str(event.get("src_device_key") or event.get("srcip") or "unknown"),
        "source_event_id": source_event_id,
        "data_classification": "observed",
        "dimensions": dimensions,
        "metrics": metrics,
        "event_excerpt": excerpt,
        "topology_context": {
            key: event.get(key) for key in (
                "srcip", "dstip", "srcintf", "dstintf", "srcintfrole", "dstintfrole",
                "service", "policyid", "policytype",
            )
        },
        "device_profile": {
            "device_name": event.get("srcname") or event.get("devname") or "",
            "src_device_key": event.get("src_device_key") or "",
            "device_role": event.get("devtype") or "",
            "vendor": event.get("srchwvendor") or "",
            "family": event.get("srcfamily") or "",
            "srcmac": event.get("srcmac") or event.get("mastersrcmac") or "",
        },
    }
