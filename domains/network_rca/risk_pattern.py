"""Bounded, deterministic aggregation of long-lived network risks.

This module deliberately sits between the raw fact store and long-term memory.
One normalized observation updates a campaign-like :class:`RiskPattern`; raw
logs are not copied into memory one record at a time.  The implementation is
pure Python and does not call an LLM or perform network IO.

The public integration surface is intentionally small:

* :func:`risk_event_from_clickhouse_row` maps a ClickHouse JSON row to a
  normalized event without side effects.
* :class:`RiskPatternStore` exposes ``ingest``, ``ingest_many``,
  ``list_patterns``, ``search`` and deterministic ``snapshot`` restoration.
* :meth:`RiskPatternStore.mark_mitigated` establishes the boundary after which
  a new observation is recorded as a recurrence.

Real, replay and drill observations have different stable keys.  Consequently
an exercise can validate the aggregation path without changing a real risk's
counts, trend or recurrence state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping


RiskProvenance = Literal["real", "replay", "drill"]
RiskStatus = Literal["active", "mitigated", "recurrent"]
RiskTrend = Literal["increasing", "stable", "decreasing", "insufficient_data"]

_PROVENANCE = frozenset({"real", "replay", "drill"})
_DVR_PORTS = frozenset({37777, 37809, 37810})


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("event timestamp is required")
    if raw.isdigit():
        number = int(raw)
        # FortiOS eventtime is normally nanoseconds since the Unix epoch.
        seconds = number / 1_000_000_000 if number > 10_000_000_000 else number
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    parsed = datetime.fromisoformat(raw.replace(" ", "T").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    normalized = "_".join(value.strip().lower().replace("-", "_").split())
    if not normalized:
        raise ValueError("risk_type is required")
    return normalized


def _network_for(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _stable_id(parts: Iterable[str]) -> str:
    payload = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    return "risk-v1-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """One normalized risk observation suitable for deterministic aggregation."""

    event_id: str
    observed_at: datetime
    risk_type: str
    scope_key: str
    target_asset: str
    provenance: RiskProvenance = "real"
    source_ip: str | None = None
    source_network: str | None = None
    target_account: str | None = None
    target_service: str | None = None
    evidence_ref: str | None = None
    source_table: str = "netops.facts"

    def __post_init__(self) -> None:
        event_id = _text(self.event_id)
        scope_key = _text(self.scope_key)
        target_asset = _text(self.target_asset)
        if event_id is None:
            raise ValueError("event_id is required for idempotent ingestion")
        if scope_key is None:
            raise ValueError("scope_key is required")
        if target_asset is None:
            raise ValueError("target_asset is required")
        if self.provenance not in _PROVENANCE:
            raise ValueError(f"unsupported provenance: {self.provenance!r}")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "risk_type", _slug(self.risk_type))
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(self, "target_asset", target_asset)
        object.__setattr__(self, "source_ip", _text(self.source_ip))
        object.__setattr__(
            self,
            "source_network",
            _text(self.source_network) or _network_for(_text(self.source_ip)),
        )
        object.__setattr__(self, "target_account", _text(self.target_account))
        service = _text(self.target_service)
        object.__setattr__(self, "target_service", service.casefold() if service else None)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref))
        object.__setattr__(self, "source_table", _text(self.source_table) or "netops.facts")

    @property
    def pattern_id(self) -> str:
        return _stable_id(
            (
                self.provenance,
                self.risk_type,
                self.scope_key,
                self.target_service or "*",
            )
        )


@dataclass(frozen=True, slots=True)
class EvidenceQueryRange:
    """Minimal range and predicates needed to re-read evidence from the fact store."""

    source_table: str
    start_at: datetime
    end_at: datetime
    filters: tuple[tuple[str, str], ...]
    sample_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_table": self.source_table,
            "start_at": _iso(self.start_at),
            "end_at": _iso(self.end_at),
            "filters": {key: value for key, value in self.filters},
            "sample_refs": list(self.sample_refs),
        }


@dataclass(slots=True)
class RiskPattern:
    """A bounded aggregate of observations that share one stable risk key."""

    pattern_id: str
    risk_type: str
    scope_key: str
    target_service: str | None
    provenance: RiskProvenance
    first_seen: datetime
    last_seen: datetime
    event_count: int = 0
    status: RiskStatus = "active"
    recurrence_count: int = 0
    mitigated_at: datetime | None = None
    last_mitigation_id: str | None = None
    _daily_counts: dict[date, int] = field(default_factory=dict, repr=False)
    _source_ips: set[str] = field(default_factory=set, repr=False)
    _source_networks: set[str] = field(default_factory=set, repr=False)
    _target_accounts: set[str] = field(default_factory=set, repr=False)
    _target_assets: set[str] = field(default_factory=set, repr=False)
    _evidence_ref_tables: dict[str, str] = field(default_factory=dict, repr=False)
    _source_tables: set[str] = field(default_factory=set, repr=False)
    _event_ids: dict[str, datetime] = field(default_factory=dict, repr=False)
    dimensions_truncated: bool = False
    dedupe_truncated: bool = False

    @property
    def active_days(self) -> int:
        return len(self._daily_counts)

    @property
    def distinct_source_count(self) -> int:
        return len(self._source_ips)

    @property
    def distinct_source_network_count(self) -> int:
        return len(self._source_networks)

    @property
    def source_ips(self) -> tuple[str, ...]:
        return tuple(sorted(self._source_ips))

    @property
    def source_networks(self) -> tuple[str, ...]:
        return tuple(sorted(self._source_networks))

    @property
    def target_accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._target_accounts))

    @property
    def target_assets(self) -> tuple[str, ...]:
        return tuple(sorted(self._target_assets))

    @property
    def trend(self) -> RiskTrend:
        if len(self._daily_counts) < 2:
            return "insufficient_data"
        start = min(self._daily_counts)
        end = max(self._daily_counts)
        days = min(14, (end - start).days + 1)
        if days < 3:
            return "insufficient_data"
        values = [
            self._daily_counts.get(end - timedelta(days=offset), 0)
            for offset in reversed(range(days))
        ]
        mean = sum(values) / len(values)
        if mean == 0:
            return "stable"
        center = (len(values) - 1) / 2
        denominator = sum((index - center) ** 2 for index in range(len(values)))
        slope = sum(
            (index - center) * (value - mean)
            for index, value in enumerate(values)
        ) / denominator
        relative = slope / mean
        if relative > 0.10:
            return "increasing"
        if relative < -0.10:
            return "decreasing"
        return "stable"

    @property
    def evidence_query_ranges(self) -> tuple[EvidenceQueryRange, ...]:
        filters: list[tuple[str, str]] = [
            ("risk_type", self.risk_type),
            ("scope_key", self.scope_key),
            ("provenance", self.provenance),
        ]
        if self.target_service:
            filters.append(("target_service", self.target_service))
        return tuple(
            EvidenceQueryRange(
                source_table=table,
                start_at=self.first_seen,
                end_at=self.last_seen,
                filters=tuple(filters),
                sample_refs=tuple(
                    sorted(
                        ref
                        for ref, ref_table in self._evidence_ref_tables.items()
                        if ref_table == table
                    )
                ),
            )
            for table in sorted(self._source_tables)
        )

    def add(
        self,
        event: RiskEvent,
        *,
        retention: timedelta,
        max_dimension_values: int,
        max_evidence_refs: int,
        max_event_ids: int,
        prune: bool = True,
    ) -> bool:
        """Add an observation, returning ``False`` when it is a duplicate."""
        if event.pattern_id != self.pattern_id:
            raise ValueError("event does not belong to this risk pattern")
        if event.event_id in self._event_ids:
            return False

        self.event_count += 1
        self.first_seen = min(self.first_seen, event.observed_at)
        self.last_seen = max(self.last_seen, event.observed_at)
        self._daily_counts[event.observed_at.date()] = (
            self._daily_counts.get(event.observed_at.date(), 0) + 1
        )
        self._bounded_add(self._source_tables, event.source_table, max_dimension_values)
        self._bounded_add(self._source_ips, event.source_ip, max_dimension_values)
        self._bounded_add(self._source_networks, event.source_network, max_dimension_values)
        self._bounded_add(self._target_accounts, event.target_account, max_dimension_values)
        self._bounded_add(self._target_assets, event.target_asset, max_dimension_values)
        if (
            event.evidence_ref
            and event.source_table in self._source_tables
            and event.evidence_ref not in self._evidence_ref_tables
        ):
            if len(self._evidence_ref_tables) >= max_evidence_refs:
                self.dimensions_truncated = True
            else:
                self._evidence_ref_tables[event.evidence_ref] = event.source_table

        if self.mitigated_at is not None:
            if event.observed_at > self.mitigated_at:
                if self.status == "mitigated":
                    self.recurrence_count += 1
                self.status = "recurrent"
        elif self.status != "recurrent":
            self.status = "active"

        self._event_ids[event.event_id] = event.observed_at
        if prune:
            self._prune(retention=retention, max_event_ids=max_event_ids)
        return True

    def _bounded_add(self, values: set[str], value: str | None, limit: int) -> None:
        if value is None or value in values:
            return
        if len(values) >= limit:
            self.dimensions_truncated = True
            return
        values.add(value)

    def _prune(self, *, retention: timedelta, max_event_ids: int) -> None:
        cutoff = (self.last_seen - retention).date()
        self._daily_counts = {
            day: count for day, count in self._daily_counts.items() if day >= cutoff
        }
        self._event_ids = {
            event_id: observed_at
            for event_id, observed_at in self._event_ids.items()
            if observed_at >= self.last_seen - retention
        }
        if len(self._event_ids) > max_event_ids:
            ordered = sorted(
                self._event_ids.items(), key=lambda item: (item[1], item[0]), reverse=True
            )[:max_event_ids]
            self._event_ids = dict(ordered)
            self.dedupe_truncated = True

    def to_dict(self, *, include_dedupe_state: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pattern_id": self.pattern_id,
            "risk_type": self.risk_type,
            "scope_key": self.scope_key,
            "target_service": self.target_service,
            "provenance": self.provenance,
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "active_days": self.active_days,
            "event_count": self.event_count,
            "distinct_source_count": self.distinct_source_count,
            "distinct_source_network_count": self.distinct_source_network_count,
            "source_ips": list(self.source_ips),
            "source_networks": list(self.source_networks),
            "target_accounts": list(self.target_accounts),
            "target_assets": list(self.target_assets),
            "trend": self.trend,
            "daily_counts": {
                day.isoformat(): self._daily_counts[day] for day in sorted(self._daily_counts)
            },
            "status": self.status,
            "recurrence_count": self.recurrence_count,
            "mitigated_at": _iso(self.mitigated_at) if self.mitigated_at else None,
            "last_mitigation_id": self.last_mitigation_id,
            "evidence_query_ranges": [item.to_dict() for item in self.evidence_query_ranges],
            "dimensions_truncated": self.dimensions_truncated,
            "dedupe_truncated": self.dedupe_truncated,
        }
        if include_dedupe_state:
            result["event_ids"] = {
                event_id: _iso(self._event_ids[event_id]) for event_id in sorted(self._event_ids)
            }
            result["source_tables"] = sorted(self._source_tables)
            result["evidence_refs"] = sorted(self._evidence_ref_tables)
            result["evidence_ref_tables"] = {
                ref: self._evidence_ref_tables[ref]
                for ref in sorted(self._evidence_ref_tables)
            }
        return result


class RiskPatternStore:
    """In-memory bounded aggregate store with deterministic snapshot support."""

    SNAPSHOT_VERSION = 1

    def __init__(
        self,
        *,
        retention: timedelta = timedelta(days=90),
        max_patterns: int = 2048,
        max_dimension_values: int = 4096,
        max_evidence_refs: int = 64,
        max_event_ids_per_pattern: int = 20_000,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        for name, value in (
            ("max_patterns", max_patterns),
            ("max_dimension_values", max_dimension_values),
            ("max_evidence_refs", max_evidence_refs),
            ("max_event_ids_per_pattern", max_event_ids_per_pattern),
        ):
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.retention = retention
        self.max_patterns = int(max_patterns)
        self.max_dimension_values = int(max_dimension_values)
        self.max_evidence_refs = int(max_evidence_refs)
        self.max_event_ids_per_pattern = int(max_event_ids_per_pattern)
        self._patterns: dict[str, RiskPattern] = {}
        self._watermark: datetime | None = None

    def ingest(self, event: RiskEvent) -> RiskPattern | None:
        """Idempotently merge one event and return its pattern if retained."""
        if not isinstance(event, RiskEvent):
            raise TypeError("ingest expects a RiskEvent")
        self._watermark = max(self._watermark or event.observed_at, event.observed_at)
        self.expire(self._watermark)

        return self._ingest_retained(event)

    def _ingest_retained(
        self, event: RiskEvent, *, prune: bool = True
    ) -> RiskPattern | None:
        """Merge after the caller has advanced the watermark and expired once."""
        assert self._watermark is not None

        pattern = self._patterns.get(event.pattern_id)
        if pattern is None:
            if event.observed_at < self._watermark - self.retention:
                return None
            pattern = RiskPattern(
                pattern_id=event.pattern_id,
                risk_type=event.risk_type,
                scope_key=event.scope_key,
                target_service=event.target_service,
                provenance=event.provenance,
                first_seen=event.observed_at,
                last_seen=event.observed_at,
            )
            self._patterns[event.pattern_id] = pattern
        pattern.add(
            event,
            retention=self.retention,
            max_dimension_values=self.max_dimension_values,
            max_evidence_refs=self.max_evidence_refs,
            max_event_ids=self.max_event_ids_per_pattern,
            prune=prune,
        )
        if prune:
            self._enforce_pattern_bound()
        return self._patterns.get(event.pattern_id)

    def ingest_many(self, events: Iterable[RiskEvent]) -> list[RiskPattern]:
        """Merge a batch and return the distinct affected patterns in stable order."""
        batch = list(events)
        if not batch:
            return []
        if any(not isinstance(event, RiskEvent) for event in batch):
            raise TypeError("ingest_many expects RiskEvent values")
        newest = max(event.observed_at for event in batch)
        self._watermark = max(self._watermark or newest, newest)
        self.expire(self._watermark)
        touched: set[str] = set()
        affected: set[str] = set()
        for event in batch:
            existing = self._patterns.get(event.pattern_id)
            duplicate = existing is not None and event.event_id in existing._event_ids
            pattern = self._ingest_retained(event, prune=False)
            if pattern is not None:
                touched.add(pattern.pattern_id)
                if not duplicate:
                    affected.add(pattern.pattern_id)
        # Retention still applies to every pattern present in the overlapping
        # query window.  Only genuinely changed patterns are returned to
        # downstream feature extraction, so replaying 30k duplicate source
        # rows cannot trigger thousands of redundant feature reassessments.
        for pattern_id in touched:
            self._patterns[pattern_id]._prune(
                retention=self.retention,
                max_event_ids=self.max_event_ids_per_pattern,
            )
        # Enforce the capacity boundary once for the complete source window.
        # Applying it after every event makes two overlapping source batches
        # repeatedly evict and recreate each other's boundary patterns.
        self._enforce_pattern_bound()
        return [self._patterns[key] for key in sorted(affected) if key in self._patterns]

    def ingest_clickhouse_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source_table: str = "netops.facts",
    ) -> list[RiskPattern]:
        """Map and merge a bounded query batch; normal traffic rows are skipped."""
        events = (
            event
            for event in (
                risk_event_from_clickhouse_row(row, source_table=source_table)
                for row in rows
            )
            if event is not None
        )
        return self.ingest_many(events)

    def get(self, pattern_id: str) -> RiskPattern | None:
        return self._patterns.get(pattern_id)

    def mark_mitigated(
        self,
        pattern_id: str,
        *,
        at: datetime,
        mitigation_id: str,
    ) -> RiskPattern:
        pattern = self._patterns[pattern_id]
        timestamp = _utc(at)
        if timestamp < pattern.first_seen:
            raise ValueError("mitigation cannot predate the first observation")
        identifier = _text(mitigation_id)
        if identifier is None:
            raise ValueError("mitigation_id is required")
        # Retrying the same write is idempotent. A newer mitigation starts a new
        # recurrence boundary after a previously recurrent campaign.
        if pattern.last_mitigation_id == identifier and pattern.mitigated_at == timestamp:
            return pattern
        if pattern.mitigated_at is not None and timestamp < pattern.mitigated_at:
            raise ValueError("mitigation time cannot move backwards")
        pattern.mitigated_at = timestamp
        pattern.last_mitigation_id = identifier
        pattern.status = "mitigated"
        return pattern

    def expire(self, now: datetime) -> tuple[str, ...]:
        cutoff = _utc(now) - self.retention
        expired = tuple(
            sorted(key for key, pattern in self._patterns.items() if pattern.last_seen < cutoff)
        )
        for key in expired:
            del self._patterns[key]
        return expired

    def list_patterns(
        self,
        *,
        status: RiskStatus | None = None,
        provenance: RiskProvenance | None = None,
        limit: int | None = None,
    ) -> list[RiskPattern]:
        rows = [
            pattern
            for pattern in self._patterns.values()
            if (status is None or pattern.status == status)
            and (provenance is None or pattern.provenance == provenance)
        ]
        rows.sort(key=lambda item: (-item.last_seen.timestamp(), item.pattern_id))
        return rows if limit is None else rows[: max(0, int(limit))]

    def search(self, query: str, *, limit: int = 20) -> list[RiskPattern]:
        terms = tuple(part for part in query.strip().lower().split() if part)
        if not terms:
            return self.list_patterns(limit=limit)

        def searchable(pattern: RiskPattern) -> str:
            return " ".join(
                (
                    pattern.pattern_id,
                    pattern.risk_type,
                    pattern.scope_key,
                    pattern.target_service or "",
                    pattern.provenance,
                    pattern.status,
                    *pattern.source_ips,
                    *pattern.source_networks,
                    *pattern.target_accounts,
                    *pattern.target_assets,
                )
            ).lower()

        matches = [
            pattern
            for pattern in self._patterns.values()
            if all(term in searchable(pattern) for term in terms)
        ]
        matches.sort(
            key=lambda item: (-item.event_count, -item.last_seen.timestamp(), item.pattern_id)
        )
        return matches[: max(0, int(limit))]

    def snapshot(self) -> dict[str, Any]:
        """Return complete JSON-serializable state with deterministic ordering."""
        return {
            "version": self.SNAPSHOT_VERSION,
            "config": {
                "retention_seconds": int(self.retention.total_seconds()),
                "max_patterns": self.max_patterns,
                "max_dimension_values": self.max_dimension_values,
                "max_evidence_refs": self.max_evidence_refs,
                "max_event_ids_per_pattern": self.max_event_ids_per_pattern,
            },
            "watermark": _iso(self._watermark) if self._watermark else None,
            "patterns": [
                self._patterns[key].to_dict(include_dedupe_state=True)
                for key in sorted(self._patterns)
            ],
        }

    def snapshot_json(self) -> str:
        return json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_snapshot(cls, raw: Mapping[str, Any]) -> "RiskPatternStore":
        if int(raw.get("version", 0)) != cls.SNAPSHOT_VERSION:
            raise ValueError("unsupported risk-pattern snapshot version")
        config = dict(raw.get("config") or {})
        store = cls(
            retention=timedelta(seconds=int(config["retention_seconds"])),
            max_patterns=int(config["max_patterns"]),
            max_dimension_values=int(config["max_dimension_values"]),
            max_evidence_refs=int(config["max_evidence_refs"]),
            max_event_ids_per_pattern=int(config["max_event_ids_per_pattern"]),
        )
        if raw.get("watermark"):
            store._watermark = _timestamp(raw["watermark"])
        for item_raw in raw.get("patterns") or []:
            item = dict(item_raw)
            pattern = RiskPattern(
                pattern_id=str(item["pattern_id"]),
                risk_type=str(item["risk_type"]),
                scope_key=str(item["scope_key"]),
                target_service=_text(item.get("target_service")),
                provenance=str(item["provenance"]),  # type: ignore[arg-type]
                first_seen=_timestamp(item["first_seen"]),
                last_seen=_timestamp(item["last_seen"]),
                event_count=int(item["event_count"]),
                status=str(item["status"]),  # type: ignore[arg-type]
                recurrence_count=int(item.get("recurrence_count", 0)),
                mitigated_at=(
                    _timestamp(item["mitigated_at"]) if item.get("mitigated_at") else None
                ),
                last_mitigation_id=_text(item.get("last_mitigation_id")),
                _daily_counts={
                    date.fromisoformat(day): int(count)
                    for day, count in dict(item.get("daily_counts") or {}).items()
                },
                _source_ips=set(item.get("source_ips") or []),
                _source_networks=set(item.get("source_networks") or []),
                _target_accounts=set(item.get("target_accounts") or []),
                _target_assets=set(item.get("target_assets") or []),
                _evidence_ref_tables={
                    str(ref): str(table)
                    for ref, table in dict(item.get("evidence_ref_tables") or {}).items()
                },
                _source_tables=set(item.get("source_tables") or []),
                _event_ids={
                    str(event_id): _timestamp(observed_at)
                    for event_id, observed_at in dict(item.get("event_ids") or {}).items()
                },
                dimensions_truncated=bool(item.get("dimensions_truncated", False)),
                dedupe_truncated=bool(item.get("dedupe_truncated", False)),
            )
            if not pattern._evidence_ref_tables and item.get("evidence_refs"):
                fallback_table = next(iter(sorted(pattern._source_tables)), "netops.facts")
                pattern._evidence_ref_tables = {
                    str(ref): fallback_table for ref in item["evidence_refs"]
                }
            if pattern.pattern_id in store._patterns:
                raise ValueError(f"duplicate pattern in snapshot: {pattern.pattern_id}")
            store._patterns[pattern.pattern_id] = pattern
        store._enforce_pattern_bound()
        return store

    def _enforce_pattern_bound(self) -> None:
        while len(self._patterns) > self.max_patterns:
            victim = min(
                self._patterns.values(), key=lambda item: (item.last_seen, item.pattern_id)
            )
            del self._patterns[victim.pattern_id]


def _row_provenance(row: Mapping[str, Any]) -> RiskProvenance:
    explicit = str(row.get("provenance") or "").strip().lower()
    if explicit in _PROVENANCE:
        return explicit  # type: ignore[return-value]
    source_kind = str(row.get("source_kind") or "").strip().lower()
    if source_kind in {"drill", "exercise", "rehearsal"}:
        return "drill"
    replay_marker = str(row.get("replay") or "").strip().lower()
    if replay_marker in {"1", "true", "yes"} or source_kind in {"simulated", "replay"}:
        return "replay"
    return "real"


def _row_risk_type(row: Mapping[str, Any]) -> str | None:
    explicit = _text(row.get("risk_type"))
    if explicit:
        return _slug(explicit)
    action = str(row.get("action") or "").strip().lower()
    status = str(row.get("status") or "").strip().lower()
    logdesc = str(row.get("logdesc") or "").strip().lower()
    message = str(row.get("msg") or "").strip().lower()
    combined = " ".join((action, status, logdesc, message))
    if "login" in combined and any(
        marker in combined for marker in ("fail", "disabled", "lockout", "invalid")
    ):
        return "credential_attack"
    port = _integer(row.get("dstport") or row.get("dst_port"))
    if action in {"deny", "blocked", "block"} and port in _DVR_PORTS:
        return "exposed_service_probe"
    if action in {"deny", "blocked", "block"}:
        return "policy_deny_activity"
    return None


def risk_event_from_clickhouse_row(
    row: Mapping[str, Any],
    *,
    source_table: str = "netops.facts",
) -> RiskEvent | None:
    """Purely map a ClickHouse JSON row to a normalized risk observation.

    Rows that do not carry a supported risk signal return ``None``.  When the
    source has no event id, a digest of the canonical row fields provides a
    deterministic replay key.  Identical records at an identical timestamp are
    therefore treated as one observation, which is the only honest distinction
    possible without a source sequence id.
    """
    risk_type = _row_risk_type(row)
    if risk_type is None:
        return None
    observed_at = _timestamp(
        row.get("event_ts") or row.get("timestamp") or row.get("at") or row.get("eventtime")
    )
    provenance = _row_provenance(row)
    source_ip = _text(row.get("srcip") or row.get("src_ip"))
    target_asset = _text(
        row.get("target_asset") or row.get("dstip") or row.get("dst_ip") or row.get("device_key")
    )
    if target_asset is None:
        raise ValueError("risk row has no target asset")
    scope_key = _text(row.get("scope_key") or row.get("device_key")) or target_asset
    port = _integer(row.get("dstport") or row.get("dst_port"))
    target_service = _text(row.get("target_service") or row.get("service") or row.get("app"))
    if target_service is None and port is not None:
        protocol = _text(row.get("proto")) or "ip"
        target_service = f"{protocol}/{port}"
    # Normalized security_events carry the attempted account in ``user`` even
    # when their explicit risk type is admin_login_failed/lockout. Flow facts
    # may use srcname for a host, so that fallback remains credential-only.
    target_account = _text(row.get("target_account") or row.get("user"))
    if target_account is None and risk_type == "credential_attack":
        target_account = _text(row.get("srcname"))

    canonical = {
        "observed_at": _iso(observed_at),
        "risk_type": risk_type,
        "scope_key": scope_key,
        "target_asset": target_asset,
        "target_service": target_service,
        "target_account": target_account,
        "source_ip": source_ip,
        "action": _text(row.get("action")),
        "logid": _text(row.get("logid")),
        "provenance": provenance,
    }
    event_id = _text(row.get("event_id") or row.get("fact_id") or row.get("id"))
    if event_id is None:
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        event_id = "ch-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    evidence_ref = _text(row.get("evidence_ref")) or f"{source_table}:{event_id}"
    return RiskEvent(
        event_id=event_id,
        observed_at=observed_at,
        risk_type=risk_type,
        scope_key=scope_key,
        target_asset=target_asset,
        provenance=provenance,
        source_ip=source_ip,
        source_network=_text(row.get("source_network")),
        target_account=target_account,
        target_service=target_service,
        evidence_ref=evidence_ref,
        source_table=source_table,
    )


__all__ = [
    "EvidenceQueryRange",
    "RiskEvent",
    "RiskPattern",
    "RiskPatternStore",
    "risk_event_from_clickhouse_row",
]
