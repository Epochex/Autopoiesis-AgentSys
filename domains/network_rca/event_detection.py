"""Deterministic event detection for the live Autopoiesis data plane.

The collector emits normalized FortiGate events.  This module turns a small,
explicit set of known conditions into incident triggers.  Open-ended diagnosis
starts after the trigger has been persisted; benchmark labels and replay-only
fields are intentionally outside this production detector.
"""
from __future__ import annotations

import hashlib
import ipaddress
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
    auth_window_seconds: int = 60
    auth_failure_threshold: int = 12
    auth_distinct_source_threshold: int = 5
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
        self._deny_alerted: set[tuple[str, ...]] = set()
        self._byte: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)
        self._auth: dict[str, deque[tuple[datetime, str, str, str]]] = defaultdict(deque)
        self._auth_alerted: set[str] = set()
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
        auth = self._admin_auth_attack(event, observed_at)
        if auth is not None:
            alerts.append(auth)
        return alerts

    def _admin_auth_attack(
        self, event: dict[str, Any], now: datetime,
    ) -> dict[str, Any] | None:
        if str(event.get("type") or "").casefold() != "event":
            return None
        combined = " ".join(str(event.get(field) or "") for field in (
            "logdesc", "msg", "action", "event_status", "reason",
        )).casefold()
        login = "login" in combined or str(event.get("action") or "").casefold() == "login"
        failed = login and any(
            marker in combined
            for marker in ("failed", "failure", "invalid", "exceed_limit", "lockout", "disabled")
        )
        if not failed:
            return None
        device = str(event.get("device_key") or event.get("devname") or "fortigate")
        source = str(event.get("srcip") or "unknown")
        user = str(event.get("user") or "")
        reason = str(event.get("reason") or "")
        bucket = self._auth[device]
        cutoff = now - timedelta(seconds=self.policy.auth_window_seconds)
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        if not bucket:
            self._auth_alerted.discard(device)
        bucket.append((now, source, user, reason))
        sources = {row[1] for row in bucket if row[1] not in {"", "unknown"}}
        lockouts = sum(
            1 for row in bucket
            if any(marker in row[3].casefold() for marker in ("exceed_limit", "lockout"))
        )
        threshold_met = bool(
            len(bucket) >= self.policy.auth_failure_threshold
            and len(sources) >= self.policy.auth_distinct_source_threshold
        )
        if not threshold_met and lockouts == 0:
            return None
        if device in self._auth_alerted:
            return None
        if not self._cooldown(f"admin_auth_attack|{device}", now):
            return None
        self._auth_alerted.add(device)
        source_counts: dict[str, int] = {}
        for _at, address, _user, _reason in bucket:
            source_counts[address] = source_counts.get(address, 0) + 1
        top_sources = sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        result = _alert(
            "admin_auth_attack_v1",
            "critical",
            event,
            now,
            {"managed_device": device, "method": str(event.get("method") or "")},
            {
                "failed_logins": len(bucket),
                "distinct_sources": len(sources),
                "lockouts": lockouts,
                "top_sources": top_sources,
                "window_sec": self.policy.auth_window_seconds,
                "failure_threshold": self.policy.auth_failure_threshold,
                "distinct_source_threshold": self.policy.auth_distinct_source_threshold,
            },
        )
        result["src_device_key"] = device
        result["event_excerpt"]["service"] = "fortigate-admin"
        return result

    def _deny_burst(self, event: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if str(event.get("action") or "").casefold() != "deny":
            return None
        destination = str(event.get("dstip") or "")
        try:
            destination_ip = ipaddress.ip_address(destination)
        except ValueError:
            destination_ip = None
        if (
            str(event.get("subtype") or "").casefold() == "local"
            and str(event.get("srcintfrole") or "").casefold() == "lan"
            and destination_ip is not None
            and (destination_ip.is_multicast or destination.endswith(".255"))
        ):
            return None
        key = tuple(str(event.get(field) or "") for field in (
            "srcip", "dstip", "dstport", "service", "policyid", "subtype",
        ))
        bucket = self._deny[key]
        cutoff = now - timedelta(seconds=self.policy.deny_window_seconds)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            self._deny_alerted.discard(key)
        bucket.append(now)
        if len(bucket) < self.policy.deny_threshold:
            return None
        if key in self._deny_alerted:
            return None
        alert_key = "deny_burst|" + "|".join(key)
        if not self._cooldown(alert_key, now):
            return None
        self._deny_alerted.add(key)
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
        "device_key", "user", "method", "event_status", "reason", "logdesc", "msg",
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
