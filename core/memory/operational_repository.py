"""Durable storage for domain-level operational memory objects.

The generic memory index stores retrieval projections.  Incident dossiers,
risk patterns and promoted network features are authoritative business records
with their own schema and lifecycle, so they are persisted separately as
versioned JSON documents.  Every committed version also enters an append-only
event stream for audit and replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, Mapping

from core.memory.postgres_scope import enter_schema, validate_schema_name


OperationalKind = Literal["incident_dossier", "risk_pattern", "network_feature"]
_KINDS = frozenset({"incident_dossier", "risk_pattern", "network_feature"})
_WRITER_LOCK = 746_617_310_630_458_477
_DEFAULT_MAX_AGGREGATE_PAYLOAD_BYTES = 8 * 1024 * 1024


class OperationalVersionConflict(RuntimeError):
    """A caller attempted to overwrite a newer operational record."""


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    kind: OperationalKind
    record_id: str
    version: int
    payload: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationalEvent(OperationalSnapshot):
    event_offset: int


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(f"unsupported operational payload value: {type(item).__name__}")

    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=encode,
    )
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("operational payload must be an object")
    return decoded


def _validate_identity(kind: str, record_id: str) -> tuple[OperationalKind, str]:
    if kind not in _KINDS:
        raise ValueError(f"unsupported operational record kind: {kind}")
    normalized_id = record_id.strip()
    if not normalized_id:
        raise ValueError("record_id must not be empty")
    return kind, normalized_id  # type: ignore[return-value]


def _event_payload(
    kind: OperationalKind,
    document: dict[str, Any],
    encoded: str | None = None,
) -> dict[str, Any]:
    """Return a bounded audit receipt for a materialized aggregate commit."""
    if kind == "incident_dossier":
        return document
    if encoded is None:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    collection_names = (
        ("patterns",)
        if kind == "risk_pattern"
        else ("features", "observations", "decisions")
    )
    return {
        "schema_version": 1,
        "event_kind": "aggregate_snapshot_commit",
        "snapshot_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "snapshot_bytes": len(encoded.encode("utf-8")),
        "counts": {
            name: len(document.get(name) or ())
            for name in collection_names
        },
    }


class InMemoryOperationalRepository:
    """Thread-safe deterministic repository used by offline and degraded modes."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], OperationalSnapshot] = {}
        self._events: list[OperationalEvent] = []
        self._lock = RLock()

    def initialize_schema(self) -> None:
        return None

    def upsert(
        self,
        kind: OperationalKind,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> OperationalSnapshot:
        kind, record_id = _validate_identity(kind, record_id)
        document = _payload(payload)
        key = (kind, record_id)
        with self._lock:
            current = self._records.get(key)
            current_version = current.version if current is not None else 0
            if expected_version is not None and expected_version != current_version:
                raise OperationalVersionConflict(
                    f"{kind}:{record_id} expected version {expected_version}, "
                    f"current version is {current_version}"
                )
            if current is not None and current.payload == document:
                return current
            now = datetime.now(timezone.utc)
            snapshot = OperationalSnapshot(
                kind=kind,
                record_id=record_id,
                version=current_version + 1,
                payload=document,
                updated_at=now,
            )
            self._records[key] = snapshot
            self._events.append(OperationalEvent(
                kind=snapshot.kind,
                record_id=snapshot.record_id,
                version=snapshot.version,
                payload=_event_payload(kind, document),
                updated_at=snapshot.updated_at,
                event_offset=len(self._events) + 1,
            ))
            return snapshot

    def get(self, kind: OperationalKind, record_id: str) -> OperationalSnapshot | None:
        kind, record_id = _validate_identity(kind, record_id)
        with self._lock:
            return self._records.get((kind, record_id))

    def payload_size(self, kind: OperationalKind, record_id: str) -> int | None:
        kind, record_id = _validate_identity(kind, record_id)
        with self._lock:
            snapshot = self._records.get((kind, record_id))
        if snapshot is None:
            return None
        return len(
            json.dumps(
                snapshot.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def load(
        self, kind: OperationalKind, *, limit: int | None = None,
    ) -> list[OperationalSnapshot]:
        kind, _ = _validate_identity(kind, "load")
        with self._lock:
            rows = sorted(
                (row for (row_kind, _), row in self._records.items() if row_kind == kind),
                key=lambda row: row.record_id,
            )
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            rows.sort(key=lambda row: (-row.updated_at.timestamp(), row.record_id))
            rows = rows[:limit]
        return rows

    def read_events(self, *, after_offset: int = 0, limit: int = 1000) -> list[OperationalEvent]:
        if after_offset < 0 or limit <= 0:
            raise ValueError("invalid event page")
        with self._lock:
            return list(self._events[after_offset:after_offset + limit])


class PostgresOperationalRepository:
    """PostgreSQL implementation with optimistic concurrency and replay events."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str | None = None,
        connect_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("dsn must not be empty")
        self._dsn = dsn
        self._schema = validate_schema_name(schema)
        self._connect_factory = connect_factory

    @staticmethod
    def schema_sql() -> str:
        return (Path(__file__).parent / "sql" / "002_operational_memory.sql").read_text(
            encoding="utf-8"
        )

    def _connect(self) -> Any:
        if self._connect_factory is not None:
            connection = self._connect_factory(self._dsn)
            enter_schema(connection, self._schema)
            return connection
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PostgreSQL persistence requires psycopg 3; install psycopg[binary]"
            ) from exc
        connection = psycopg.connect(
            self._dsn,
            connect_timeout=5,
            options=(
                "-c statement_timeout=30000 "
                "-c lock_timeout=5000 "
                "-c idle_in_transaction_session_timeout=30000"
            ),
        )
        enter_schema(connection, self._schema)
        return connection

    def initialize_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.schema_sql())

    def upsert(
        self,
        kind: OperationalKind,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> OperationalSnapshot:
        kind, record_id = _validate_identity(kind, record_id)
        document = _payload(payload)
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded_bytes = encoded.encode("utf-8")
        digest = hashlib.sha256(encoded_bytes).hexdigest()
        if kind != "incident_dossier":
            maximum = max(
                1,
                int(os.getenv(
                    "AUTOPOIESIS_OPERATIONAL_MAX_SNAPSHOT_BYTES",
                    str(_DEFAULT_MAX_AGGREGATE_PAYLOAD_BYTES),
                )),
            )
            if len(encoded_bytes) > maximum:
                raise ValueError(
                    f"{kind} snapshot is {len(encoded_bytes)} bytes; limit is {maximum}"
                )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITER_LOCK,))
                cursor.execute(
                    "SELECT version, updated_at, payload_sha256 "
                    "FROM operational_memory_records "
                    "WHERE kind=%s AND record_id=%s FOR UPDATE",
                    (kind, record_id),
                )
                row = cursor.fetchone()
                current_version = int(row[0]) if row is not None else 0
                if expected_version is not None and expected_version != current_version:
                    raise OperationalVersionConflict(
                        f"{kind}:{record_id} expected version {expected_version}, "
                        f"current version is {current_version}"
                    )
                # A fixed-size digest makes unchanged writes constant-size. The
                # previous JSONB equality query sent the entire snapshot before
                # PostgreSQL could answer and could hold the writer transaction
                # open indefinitely when the process was under memory pressure.
                if row is not None and row[2] == digest:
                    return OperationalSnapshot(
                        kind, record_id, current_version, document, row[1]
                    )
                version = current_version + 1
                cursor.execute(
                    """
                    INSERT INTO operational_memory_records(
                      kind, record_id, version, payload, payload_sha256
                    )
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT(kind, record_id) DO UPDATE SET
                      version=EXCLUDED.version, payload=EXCLUDED.payload,
                      payload_sha256=EXCLUDED.payload_sha256,
                      updated_at=clock_timestamp()
                    RETURNING updated_at
                    """,
                    (kind, record_id, version, encoded, digest),
                )
                updated_at = cursor.fetchone()[0]
                event_encoded = json.dumps(
                    _event_payload(kind, document, encoded),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor.execute(
                    "INSERT INTO operational_memory_events(kind, record_id, version, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (kind, record_id, version, event_encoded),
                )
                return OperationalSnapshot(kind, record_id, version, document, updated_at)

    def get(self, kind: OperationalKind, record_id: str) -> OperationalSnapshot | None:
        kind, record_id = _validate_identity(kind, record_id)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT version, payload, updated_at FROM operational_memory_records "
                    "WHERE kind=%s AND record_id=%s",
                    (kind, record_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
        return OperationalSnapshot(kind, record_id, int(row[0]), payload, row[2])

    def payload_size(self, kind: OperationalKind, record_id: str) -> int | None:
        kind, record_id = _validate_identity(kind, record_id)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    # Bound the decoded document, not PostgreSQL's compressed
                    # on-disk representation. A 33 MB JSON document can fit in
                    # an 8 MB TOAST value and still expand several times again
                    # as Python objects.
                    "SELECT octet_length(payload::text) FROM operational_memory_records "
                    "WHERE kind=%s AND record_id=%s",
                    (kind, record_id),
                )
                row = cursor.fetchone()
        return int(row[0]) if row is not None else None

    def load(
        self, kind: OperationalKind, *, limit: int | None = None,
    ) -> list[OperationalSnapshot]:
        kind, _ = _validate_identity(kind, "load")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if limit is None:
                    cursor.execute(
                        "SELECT record_id, version, payload, updated_at "
                        "FROM operational_memory_records WHERE kind=%s ORDER BY record_id",
                        (kind,),
                    )
                else:
                    cursor.execute(
                        "SELECT record_id, version, payload, updated_at "
                        "FROM operational_memory_records WHERE kind=%s "
                        "ORDER BY updated_at DESC, record_id LIMIT %s",
                        (kind, limit),
                    )
                rows = cursor.fetchall()
        return [
            OperationalSnapshot(
                kind, row[0], int(row[1]),
                json.loads(row[2]) if isinstance(row[2], str) else dict(row[2]), row[3],
            )
            for row in rows
        ]

    def read_events(self, *, after_offset: int = 0, limit: int = 1000) -> list[OperationalEvent]:
        if after_offset < 0 or limit <= 0:
            raise ValueError("invalid event page")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_offset, kind, record_id, version, payload, occurred_at "
                    "FROM operational_memory_events WHERE event_offset>%s "
                    "ORDER BY event_offset LIMIT %s",
                    (after_offset, limit),
                )
                rows = cursor.fetchall()
        return [
            OperationalEvent(
                kind=row[1], record_id=row[2], version=int(row[3]),
                payload=json.loads(row[4]) if isinstance(row[4], str) else dict(row[4]),
                updated_at=row[5], event_offset=int(row[0]),
            )
            for row in rows
        ]
