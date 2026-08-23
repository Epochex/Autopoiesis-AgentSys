"""Memory erasure contracts for tombstone redaction and explicit purge."""
from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.memory.postgres_repository import PostgresMemoryRepository
from core.memory.store import MemoryRecord, TieredMemoryStore


_DSN = os.environ.get("AUTOPOIESIS_TEST_POSTGRES_DSN")


def _sensitive_record(memory_id: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tier="semantic",
        text="secret-needle customer gateway failed after certificate rotation",
        tags=["secret-tag", "certificate"],
        asset_ids=["secret-asset"],
        evidence_ids=["secret-evidence"],
        source_trace_ids=["secret-trace"],
        evidence_snapshot=[{"raw": "secret-payload"}],
        links=["secret-link"],
        relations=[
            {
                "target_id": "secret-target",
                "relation_type": "similar_to",
                "evidence_ids": ["secret-relation-evidence"],
            }
        ],
        metric_window={"secret-metric": "secret-value"},
        baseline_delta={"secret-baseline": 3.0},
        config_version="secret-config",
    )


class _VectorProjection:
    def __init__(self) -> None:
        self.documents: dict[str, str] = {}
        self.deleted: list[str] = []

    def upsert(self, memory_id: str, text: str, **_kwargs) -> bool:
        self.documents[memory_id] = text
        return True

    def delete(self, memory_id: str, **_kwargs) -> bool:
        self.documents.pop(memory_id, None)
        self.deleted.append(memory_id)
        return True

    def search(self, _query: str, k: int = 10):
        return [
            SimpleNamespace(memory_id=memory_id, score=0.9, version=1)
            for memory_id in list(self.documents)[:k]
        ]

    def compact(self) -> int:
        return len(self.documents)

    def should_compact(self) -> bool:
        return False

    def health(self) -> dict[str, int]:
        return {"live_documents": len(self.documents)}


class _PurgeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def purge(
        self,
        memory_id: str,
        reason: str,
        *,
        actor: str,
        allow_purge: bool = False,
    ) -> SimpleNamespace:
        if not allow_purge:
            raise PermissionError("memory purge requires allow_purge=True")
        self.calls.append((memory_id, reason, actor))
        return SimpleNamespace(event_count=1)


def test_store_delete_defaults_to_redaction_and_removes_both_indexes():
    record = _sensitive_record("local-redact")
    vector = _VectorProjection()
    store = TieredMemoryStore(vector_index=vector)
    store.add(record)

    assert "local-redact" in vector.documents
    assert store.retrieve(["secret-needle"], [], limit_per_tier=5)["semantic"] == [record]

    assert store.delete("local-redact", "user correction")
    tombstone = store.get("local-redact")
    assert tombstone is not None
    assert tombstone.text == "[REDACTED]"
    assert tombstone.quarantined
    assert tombstone.redaction_reason == "user correction"
    assert tombstone.redacted_at is not None
    assert tombstone.tags == []
    assert tombstone.asset_ids == []
    assert tombstone.evidence_ids == []
    assert tombstone.source_trace_ids == []
    assert tombstone.evidence_snapshot == []
    assert tombstone.links == []
    assert tombstone.relations == []
    assert tombstone.metric_window == {}
    assert tombstone.baseline_delta == {}
    assert store.active() == []
    assert store.retrieve(["secret-needle"], ["secret-asset"])["semantic"] == []
    assert "local-redact" not in vector.documents
    assert "local-redact" in vector.deleted


def test_purge_is_separate_and_closed_by_default():
    repository = PostgresMemoryRepository("postgresql://unused/unused")
    with pytest.raises(PermissionError, match="allow_purge"):
        repository.purge("m1", "legal request", actor="admin")
    with pytest.raises(ValueError, match="actor"):
        repository.purge("m1", "legal request", actor=" ", allow_purge=True)
    with pytest.raises(ValueError, match="reason"):
        repository.redact("m1", " ")


def test_store_purge_commits_first_then_removes_local_indexes():
    repository = _PurgeRepository()
    vector = _VectorProjection()
    record = _sensitive_record("local-purge")
    store = TieredMemoryStore(repository=repository, vector_index=vector)  # type: ignore[arg-type]
    store.add(record)

    with pytest.raises(PermissionError, match="allow_purge"):
        store.purge("local-purge", "approved", actor="admin")
    assert store.get("local-purge") is record
    assert "local-purge" in vector.documents

    assert store.purge(
        "local-purge", "approved", actor="admin", allow_purge=True
    )
    assert repository.calls == [("local-purge", "approved", "admin")]
    assert store.get("local-purge") is None
    assert store.active() == []
    assert "local-purge" not in vector.documents


def test_schema_exposes_narrow_erasure_paths_and_keeps_mutation_guards():
    sql = PostgresMemoryRepository.schema_sql()
    assert "event_type IN ('UPSERT', 'QUARANTINE', 'REDACT')" in sql
    assert "ON DELETE CASCADE" in sql
    assert "CREATE OR REPLACE FUNCTION redact_memory" in sql
    assert "CREATE OR REPLACE FUNCTION purge_memory" in sql
    assert "CREATE TABLE IF NOT EXISTS memory_purge_log" in sql
    assert "NEW.record IS DISTINCT FROM authorized_record" in sql
    assert "NEW.event_offset IS DISTINCT FROM OLD.event_offset" in sql
    assert "memory_events is append-only" in sql
    assert "memory_purge_log is append-only" in sql


@pytest.mark.skipif(not _DSN, reason="AUTOPOIESIS_TEST_POSTGRES_DSN is not configured")
def test_real_postgres_redact_purge_and_append_only_guards():
    psycopg = pytest.importorskip("psycopg")
    assert _DSN is not None
    schema_name = f"memory_erasure_{uuid4().hex}"
    with psycopg.connect(_DSN, autocommit=True) as admin_connection:
        admin_connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(
                psycopg.sql.Identifier(schema_name)
            )
        )
    test_dsn = psycopg.conninfo.make_conninfo(
        _DSN, options=f"-csearch_path={schema_name}"
    )

    try:
        repository = PostgresMemoryRepository(test_dsn)
        repository.initialize_schema()
        redact_id = f"redact-{uuid4()}"
        purge_id = f"purge-{uuid4()}"
        repository.upsert(_sensitive_record(redact_id), expected_version=0)
        repository.upsert(_sensitive_record(purge_id), expected_version=0)

        vector = _VectorProjection()
        store = TieredMemoryStore.from_repository(repository)
        for record in store.active():
            vector.documents[record.memory_id] = store.vector_document(record)
        store.attach_vector_index(vector)

        assert store.redact(redact_id, "incorrect personal data")
        tombstone = store.get(redact_id)
        assert tombstone is not None
        assert tombstone.quarantined and tombstone.text == "[REDACTED]"
        assert store.active() == [store.get(purge_id)]
        assert store.retrieve(["secret-needle"], [], limit_per_tier=10)["semantic"] == [
            store.get(purge_id)
        ]
        assert redact_id not in vector.documents

        events = [event for event in repository.read_events(limit=10_000) if event.memory_id == redact_id]
        assert [event.event_type for event in events] == ["UPSERT", "REDACT"]
        assert all(event.record.text == "[REDACTED]" for event in events)
        assert all(event.record.tags == [] for event in events)
        assert all(event.record.evidence_snapshot == [] for event in events)
        assert all(event.record.redaction_reason == "incorrect personal data" for event in events)

        with psycopg.connect(test_dsn) as connection:
            raw = connection.execute(
                """
                SELECT count(*)
                FROM memory_events
                WHERE memory_id = %s AND record::text LIKE '%%secret%%'
                """,
                (redact_id,),
            ).fetchone()
            assert raw is not None and raw[0] == 0

        last_offset = events[-1].event_offset
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with psycopg.connect(test_dsn) as connection:
                connection.execute(
                    "UPDATE memory_events SET record = record WHERE event_offset = %s",
                    (last_offset,),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with psycopg.connect(test_dsn) as connection:
                connection.execute(
                    "DELETE FROM memory_events WHERE event_offset = %s",
                    (last_offset,),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with psycopg.connect(test_dsn) as connection:
                connection.execute(
                    "DELETE FROM memory_events WHERE memory_id = 'does-not-exist'"
                )

        purge_event_count = len(
            [event for event in repository.read_events(limit=10_000) if event.memory_id == purge_id]
        )
        with pytest.raises(PermissionError, match="allow_purge"):
            repository.purge(purge_id, "approved erasure", actor="privacy-admin")

        assert store.purge(
            purge_id,
            "approved erasure",
            actor="privacy-admin",
            allow_purge=True,
        )
        assert store.get(purge_id) is None
        assert repository.get(purge_id) is None
        assert not [
            event for event in repository.read_events(limit=10_000) if event.memory_id == purge_id
        ]
        assert purge_id not in vector.documents
        log = repository.read_purge_log(limit=1)
        assert len(log) == 1
        assert log[0].actor == "privacy-admin"
        assert log[0].reason == "approved erasure"
        assert log[0].event_count == purge_event_count

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with psycopg.connect(test_dsn) as connection:
                connection.execute(
                    "DELETE FROM memory_purge_log WHERE purge_id = %s",
                    (log[0].purge_id,),
                )
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin_connection:
            admin_connection.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )
