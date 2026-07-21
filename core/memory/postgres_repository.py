"""PostgreSQL persistence for memory state and replayable index changes.

``memory_records`` is the current snapshot used at process startup, while
``memory_events`` is an append-only stream consumed by incremental indexes.
Every write updates both in one transaction.  The PostgreSQL driver is imported
only when a connection is opened, so the deterministic in-process core keeps no
mandatory database dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from core.memory.store import MemoryRecord


EventType = Literal["UPSERT", "QUARANTINE"]

# Serialises event writers so an offset can never become visible before a lower
# offset commits.  That property is required for safe consumer checkpoints;
# PostgreSQL sequences alone do not guarantee commit order.
_EVENT_WRITER_LOCK = 746_617_310_630_458_476


class MemoryVersionConflict(RuntimeError):
    """The caller tried to replace a memory version that is no longer current."""


class CheckpointError(ValueError):
    """An index checkpoint moved backwards or beyond the event high-water mark."""


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    memory_id: str
    version: int
    event_offset: int | None
    event_type: EventType | None

    @property
    def written(self) -> bool:
        return self.event_offset is not None


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    event_offset: int
    memory_id: str
    version: int
    event_type: EventType
    record: MemoryRecord
    occurred_at: datetime


def _record_payload(record: MemoryRecord) -> dict[str, Any]:
    """Return a JSON-safe snapshot containing every declared MemoryRecord field."""
    # JSON round-tripping catches NaN/Infinity and detaches mutable lists/dicts.
    encoded = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return json.loads(encoded)


def _record_json(record: MemoryRecord) -> str:
    return json.dumps(
        _record_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_record(value: Any) -> MemoryRecord:
    if isinstance(value, str):
        value = json.loads(value)
    return MemoryRecord.model_validate(value)


class PostgresMemoryRepository:
    """Transactional repository for durable memory and index event replay.

    ``expected_version`` is optional for administrative/offline writes.  Pass it
    in online read-modify-write paths to reject lost updates.  Version ``0`` may
    be used to assert that a record does not exist yet.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("dsn must not be empty")
        self._dsn = dsn
        self._connect_factory = connect_factory

    @staticmethod
    def schema_sql() -> str:
        return (Path(__file__).parent / "sql" / "001_memory.sql").read_text(encoding="utf-8")

    def _connect(self) -> Any:
        if self._connect_factory is not None:
            return self._connect_factory(self._dsn)
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised without db extra
            raise RuntimeError(
                "PostgreSQL persistence requires psycopg 3; install psycopg[binary]"
            ) from exc
        return psycopg.connect(self._dsn)

    def initialize_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.schema_sql())

    @staticmethod
    def _lock_event_writer(cursor: Any) -> None:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_WRITER_LOCK,))

    @staticmethod
    def _select_for_update(cursor: Any, memory_id: str) -> tuple[int, Any] | None:
        cursor.execute(
            "SELECT version, record FROM memory_records WHERE memory_id = %s FOR UPDATE",
            (memory_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _check_expected(memory_id: str, actual: int | None, expected: int | None) -> None:
        if expected is None:
            return
        comparable_actual = actual if actual is not None else 0
        if expected != comparable_actual:
            raise MemoryVersionConflict(
                f"memory {memory_id!r} expected version {expected}, "
                f"current version is {comparable_actual}"
            )

    def _write_locked(
        self,
        cursor: Any,
        record: MemoryRecord,
        *,
        expected_version: int | None,
        requested_event_type: EventType | None,
        skip_unchanged: bool,
    ) -> MemoryWrite:
        current = self._select_for_update(cursor, record.memory_id)
        current_version = int(current[0]) if current is not None else None
        self._check_expected(record.memory_id, current_version, expected_version)

        payload = _record_payload(record)
        if current is not None:
            current_record = _record_payload(_decode_record(current[1]))
            if skip_unchanged and current_record == payload:
                return MemoryWrite(record.memory_id, current_version or 0, None, None)

        version = 1 if current_version is None else current_version + 1
        if requested_event_type is None:
            was_quarantined = (
                bool(_decode_record(current[1]).quarantined) if current is not None else False
            )
            event_type: EventType = (
                "QUARANTINE" if record.quarantined and not was_quarantined else "UPSERT"
            )
        else:
            event_type = requested_event_type

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        cursor.execute(
            """
            INSERT INTO memory_records (memory_id, version, record)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (memory_id) DO UPDATE
            SET version = EXCLUDED.version,
                record = EXCLUDED.record,
                updated_at = clock_timestamp()
            """,
            (record.memory_id, version, payload_json),
        )
        cursor.execute(
            """
            INSERT INTO memory_events (memory_id, version, event_type, record)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING event_offset
            """,
            (record.memory_id, version, event_type, payload_json),
        )
        event_offset = int(cursor.fetchone()[0])
        return MemoryWrite(record.memory_id, version, event_offset, event_type)

    def upsert(
        self,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
    ) -> MemoryWrite:
        """Insert or replace one snapshot and append its event atomically."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._lock_event_writer(cursor)
                return self._write_locked(
                    cursor,
                    record,
                    expected_version=expected_version,
                    requested_event_type="UPSERT",
                    skip_unchanged=True,
                )

    def sync_records(self, records: Sequence[MemoryRecord]) -> list[MemoryWrite]:
        """Flush a consolidation result in one transaction.

        Records are locked in stable id order to avoid cross-process deadlocks.
        Byte-independent JSONB equality suppresses unchanged versions/events.
        """
        by_id: dict[str, MemoryRecord] = {}
        for record in records:
            if record.memory_id in by_id:
                raise ValueError(f"duplicate memory_id in sync: {record.memory_id}")
            by_id[record.memory_id] = record
        if not by_id:
            return []

        writes_by_id: dict[str, MemoryWrite] = {}
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._lock_event_writer(cursor)
                for memory_id in sorted(by_id):
                    writes_by_id[memory_id] = self._write_locked(
                        cursor,
                        by_id[memory_id],
                        expected_version=None,
                        requested_event_type=None,
                        skip_unchanged=True,
                    )
        return [writes_by_id[record.memory_id] for record in records]

    def quarantine(
        self,
        memory_id: str,
        reason: str,
        *,
        expected_version: int | None = None,
    ) -> MemoryWrite:
        """Quarantine the current record and append the new full snapshot."""
        reason = reason.strip()
        if not reason:
            raise ValueError("quarantine reason must not be empty")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._lock_event_writer(cursor)
                current = self._select_for_update(cursor, memory_id)
                if current is None:
                    self._check_expected(memory_id, None, expected_version)
                    raise KeyError(memory_id)
                version, raw_record = current
                self._check_expected(memory_id, int(version), expected_version)
                record = _decode_record(raw_record)
                record.quarantined = True
                tag = f"quarantine:{reason}"
                if tag not in record.tags:
                    record.tags.append(tag)
                # The row is already locked and expected_version was checked.
                return self._write_locked(
                    cursor,
                    record,
                    expected_version=int(version),
                    requested_event_type="QUARANTINE",
                    skip_unchanged=True,
                )

    def load_records(self, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        where = "" if include_quarantined else "WHERE NOT (record ->> 'quarantined')::boolean"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT record FROM memory_records {where} ORDER BY memory_id")
                return [_decode_record(row[0]) for row in cursor.fetchall()]

    def get(self, memory_id: str) -> tuple[MemoryRecord, int] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT record, version FROM memory_records WHERE memory_id = %s",
                    (memory_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return _decode_record(row[0]), int(row[1])

    def read_events(self, *, after_offset: int = 0, limit: int = 1_000) -> list[MemoryEvent]:
        if after_offset < 0:
            raise ValueError("after_offset must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_offset, memory_id, version, event_type, record, occurred_at
                    FROM memory_events
                    WHERE event_offset > %s
                    ORDER BY event_offset
                    LIMIT %s
                    """,
                    (after_offset, limit),
                )
                return [
                    MemoryEvent(
                        event_offset=int(row[0]),
                        memory_id=row[1],
                        version=int(row[2]),
                        event_type=row[3],
                        record=_decode_record(row[4]),
                        occurred_at=row[5],
                    )
                    for row in cursor.fetchall()
                ]

    def get_checkpoint(self, index_name: str) -> int:
        if not index_name.strip():
            raise ValueError("index_name must not be empty")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_offset FROM index_checkpoints WHERE index_name = %s",
                    (index_name,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row is not None else 0

    def advance_checkpoint(self, index_name: str, event_offset: int) -> int:
        """Move an index checkpoint monotonically within the committed event log."""
        if not index_name.strip():
            raise ValueError("index_name must not be empty")
        if event_offset < 0:
            raise ValueError("event_offset must be non-negative")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._lock_event_writer(cursor)
                cursor.execute("SELECT COALESCE(MAX(event_offset), 0) FROM memory_events")
                high_water = int(cursor.fetchone()[0])
                if event_offset > high_water:
                    raise CheckpointError(
                        f"checkpoint {event_offset} is beyond event high-water mark {high_water}"
                    )
                cursor.execute(
                    """
                    INSERT INTO index_checkpoints (index_name, event_offset)
                    VALUES (%s, %s)
                    ON CONFLICT (index_name) DO UPDATE
                    SET event_offset = EXCLUDED.event_offset,
                        updated_at = clock_timestamp()
                    WHERE index_checkpoints.event_offset <= EXCLUDED.event_offset
                    RETURNING event_offset
                    """,
                    (index_name, event_offset),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT event_offset FROM index_checkpoints WHERE index_name = %s",
                        (index_name,),
                    )
                    current = int(cursor.fetchone()[0])
                    raise CheckpointError(
                        f"checkpoint for {index_name!r} cannot move from {current} to {event_offset}"
                    )
                return int(row[0])
