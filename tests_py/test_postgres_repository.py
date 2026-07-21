"""PostgreSQL memory repository unit contract and optional real-db checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from core.memory.postgres_repository import (
    CheckpointError,
    MemoryVersionConflict,
    PostgresMemoryRepository,
    _decode_record,
    _record_json,
    _record_payload,
)
from core.memory.store import MemoryRecord, TieredMemoryStore


def _record(memory_id: str, *, text: str = "链路恢复", quarantined: bool = False) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tier="semantic",
        text=text,
        tags=["支付", "gateway"],
        asset_ids=["gw-1"],
        evidence_ids=["ev-1"],
        confidence=0.875,
        quarantined=quarantined,
        source_trace_ids=["trace-1"],
        evidence_snapshot=[{"probe": "tcp", "ok": True}],
        links=["m-parent"],
        importance=2.5,
        strength=0.75,
        access_count=3,
        superseded_by=None,
    )


def test_record_json_round_trip_preserves_every_model_field():
    record = _record("中文-memory")
    payload = _record_payload(record)
    assert set(payload) == set(MemoryRecord.model_fields)
    assert _decode_record(json.loads(_record_json(record))) == record
    assert "中文" in _record_json(record)


def test_schema_has_state_event_checkpoint_and_integrity_constraints():
    sql = PostgresMemoryRepository.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS memory_records" in sql
    assert "CREATE TABLE IF NOT EXISTS memory_events" in sql
    assert "GENERATED ALWAYS AS IDENTITY" in sql
    assert "UNIQUE (memory_id, version)" in sql
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in sql
    assert "memory_events_arrays_ck" in sql
    assert "CREATE TABLE IF NOT EXISTS index_checkpoints" in sql
    assert "CHECK (event_offset >= 0)" in sql
    for field in MemoryRecord.model_fields:
        assert f"'{field}'" in sql or field in {"memory_id"}


def test_constructor_does_not_import_or_connect_to_psycopg():
    repository = PostgresMemoryRepository("postgresql://unused/unused")
    assert repository.schema_sql() == Path(
        "core/memory/sql/001_memory.sql"
    ).read_text(encoding="utf-8")


def test_local_argument_guards_run_before_database_access():
    repository = PostgresMemoryRepository("postgresql://unused/unused")
    with pytest.raises(ValueError, match="after_offset"):
        repository.read_events(after_offset=-1)
    with pytest.raises(ValueError, match="limit"):
        repository.read_events(limit=0)
    with pytest.raises(ValueError, match="index_name"):
        repository.get_checkpoint(" ")
    with pytest.raises(ValueError, match="reason"):
        repository.quarantine("m1", " ")
    with pytest.raises(ValueError, match="duplicate"):
        repository.sync_records([_record("m1"), _record("m1")])
    with pytest.raises(ValueError, match="missing memory ids"):
        repository.sync_records([_record("m1")], expected_versions={})


_DSN = os.environ.get("AUTOPOIESIS_TEST_POSTGRES_DSN")


@pytest.mark.skipif(not _DSN, reason="AUTOPOIESIS_TEST_POSTGRES_DSN is not configured")
def test_real_postgres_commit_restart_conflict_replay_and_checkpoint():
    psycopg = pytest.importorskip("psycopg")
    assert _DSN is not None
    prefix = f"pytest-{uuid4()}"
    schema_name = f"memory_test_{uuid4().hex}"
    with psycopg.connect(_DSN, autocommit=True) as admin_connection:
        admin_connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema_name))
        )
    test_dsn = psycopg.conninfo.make_conninfo(_DSN, options=f"-csearch_path={schema_name}")
    repository = PostgresMemoryRepository(test_dsn)
    repository.initialize_schema()
    first_id = f"{prefix}-first"
    second_id = f"{prefix}-second"
    checkpoint_name = f"{prefix}-hnsw"

    try:
        created = repository.upsert(_record(first_id), expected_version=0)
        assert created.version == 1 and created.written

        # A new repository instance simulates process restart and loads JSONB state.
        restarted = PostgresMemoryRepository(test_dsn)
        loaded, version = restarted.get(first_id) or (None, None)
        assert loaded == _record(first_id)
        assert version == 1

        # PostgreSQL is the fact source; a new application store rebuilds its
        # derived lexical and asset indexes from the committed snapshots.
        restored_store = TieredMemoryStore.from_repository(restarted)
        assert [record.memory_id for record in restored_store.retrieve(
            ["支付"], ["gw-1"], limit_per_tier=5
        )["semantic"]] == [first_id]

        event_count = len([e for e in restarted.read_events() if e.memory_id == first_id])
        with pytest.raises(MemoryVersionConflict):
            restarted.upsert(_record(first_id, text="stale"), expected_version=0)
        assert restarted.get(first_id) == (loaded, 1)
        assert len([e for e in restarted.read_events() if e.memory_id == first_id]) == event_count

        # Two writers race from version 1: row locking plus expected_version lets one win.
        def competing_write(text: str):
            return PostgresMemoryRepository(test_dsn).upsert(
                _record(first_id, text=text), expected_version=1
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = []
            for future in [pool.submit(competing_write, "writer-a"), pool.submit(competing_write, "writer-b")]:
                try:
                    outcomes.append(future.result())
                except MemoryVersionConflict as exc:
                    outcomes.append(exc)
        assert sum(not isinstance(item, Exception) for item in outcomes) == 1
        assert sum(isinstance(item, MemoryVersionConflict) for item in outcomes) == 1
        assert restarted.get(first_id)[1] == 2  # type: ignore[index]

        batch = restarted.sync_records(
            [restarted.get(first_id)[0], _record(second_id)]  # type: ignore[index]
        )
        assert not batch[0].written
        assert batch[1].version == 1 and batch[1].written

        quarantined = restarted.quarantine(second_id, "superseded", expected_version=1)
        assert quarantined.version == 2
        assert restarted.get(second_id)[0].quarantined  # type: ignore[index]
        assert second_id not in {
            record.memory_id for record in restarted.load_records(include_quarantined=False)
        }

        # Store-level writes carry the versions loaded at startup. Dirty
        # detection keeps unrelated records out of the CAS batch, so two
        # processes may safely change different memories.
        left = TieredMemoryStore.from_repository(restarted)
        right = TieredMemoryStore.from_repository(restarted)
        left.get(first_id).text = "left changed first"  # type: ignore[union-attr]
        right.get(second_id).text = "right changed second"  # type: ignore[union-attr]
        assert left.flush()[0].written
        assert right.flush()[0].written

        # Two stores that loaded the same version cannot silently overwrite one
        # another. A record sorted before the stale one proves that a late
        # conflict rolls the complete transaction back, including its event.
        winner = TieredMemoryStore.from_repository(restarted)
        stale = TieredMemoryStore.from_repository(restarted)
        winner.get(first_id).text = "winner"  # type: ignore[union-attr]
        stale.get(first_id).text = "stale"  # type: ignore[union-attr]
        rollback_id = f"{prefix}-aaa-rollback"
        stale.add(_record(rollback_id))
        winner.flush()
        events_before_conflict = len(restarted.read_events(limit=10_000))
        with pytest.raises(MemoryVersionConflict):
            stale.flush()
        assert restarted.get(rollback_id) is None
        assert len(restarted.read_events(limit=10_000)) == events_before_conflict
        assert restarted.get(first_id)[0].text == "winner"  # type: ignore[index]

        # Full-snapshot events reconstruct exactly the latest state after restart.
        relevant = [
            event for event in restarted.read_events(after_offset=0, limit=10_000)
            if event.memory_id.startswith(prefix)
        ]
        assert [event.event_offset for event in relevant] == sorted(
            event.event_offset for event in relevant
        )
        replayed = {event.memory_id: event.record for event in relevant}
        assert replayed[first_id] == restarted.get(first_id)[0]  # type: ignore[index]
        assert replayed[second_id] == restarted.get(second_id)[0]  # type: ignore[index]

        last_offset = relevant[-1].event_offset
        assert restarted.advance_checkpoint(checkpoint_name, last_offset) == last_offset
        assert restarted.get_checkpoint(checkpoint_name) == last_offset
        with pytest.raises(CheckpointError):
            restarted.advance_checkpoint(checkpoint_name, last_offset - 1)
        with pytest.raises(CheckpointError):
            restarted.advance_checkpoint(checkpoint_name, last_offset + 10_000_000)

        # The database itself rejects event rewriting, not just this repository API.
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with psycopg.connect(test_dsn) as connection:
                connection.execute(
                    "UPDATE memory_events SET event_type = 'UPSERT' WHERE event_offset = %s",
                    (last_offset,),
                )
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin_connection:
            admin_connection.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )


@pytest.mark.skipif(not _DSN, reason="AUTOPOIESIS_TEST_POSTGRES_DSN is not configured")
def test_service_restart_recalls_committed_memory_through_hnsw(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("faiss")
    psycopg = pytest.importorskip("psycopg")
    from domains.network_rca.factory import build_network_rca_service, load_seed_cases

    assert _DSN is not None
    schema_name = f"service_restart_{uuid4().hex}"
    with psycopg.connect(_DSN, autocommit=True) as admin_connection:
        admin_connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema_name))
        )
    test_dsn = psycopg.conninfo.make_conninfo(_DSN, options=f"-csearch_path={schema_name}")

    class _Embedder:
        dimension = 8
        model_id = "postgres-e2e-8d"

        @staticmethod
        def _encode(texts):
            rows = []
            for text in texts:
                vector = np.ones(8, dtype="float32")
                for index, value in enumerate(text.encode("utf-8")):
                    vector[index % 8] += (value % 29) / 29
                rows.append(vector)
            return np.asarray(rows, dtype="float32")

        def embed_documents(self, texts):
            return self._encode(texts)

        def embed_queries(self, texts):
            return self._encode(texts)

    try:
        case = load_seed_cases()[0]
        first = build_network_rca_service(
            tmp_path / "first.jsonl",
            memory_dsn=test_dsn,
            vector_memory_enabled=True,
            memory_embedder=_Embedder(),
            start_maintenance=False,
        )
        _, first_report = first.diagnose(case)
        first_run_id = first.last_run_id
        first.close()

        restarted = build_network_rca_service(
            tmp_path / "restarted.jsonl",
            memory_dsn=test_dsn,
            vector_memory_enabled=True,
            memory_embedder=_Embedder(),
            start_maintenance=False,
        )
        _, second_report = restarted.diagnose(case)
        recalled = next(
            event.payload for event in restarted._run_events if event.kind == "memory_read"
        )

        assert first_report.passed and second_report.passed
        assert restarted.last_run_id != first_run_id
        assert sum(len(ids) for ids in recalled.values()) > 5
        assert restarted.health()["memory_index"]["vector"]["base"] >= 8
        restarted.close()
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin_connection:
            admin_connection.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )
