from __future__ import annotations

import pytest

from core.memory.operational_repository import (
    InMemoryOperationalRepository,
    OperationalVersionConflict,
    PostgresOperationalRepository,
)


def test_in_memory_repository_is_idempotent_and_versioned() -> None:
    repo = InMemoryOperationalRepository()
    first = repo.upsert("incident_dossier", "inc-1", {"status": "open"})
    same = repo.upsert("incident_dossier", "inc-1", {"status": "open"})
    changed = repo.upsert(
        "incident_dossier", "inc-1", {"status": "confirmed"}, expected_version=1
    )

    assert (first.version, same.version, changed.version) == (1, 1, 2)
    assert len(repo.read_events()) == 2
    assert repo.get("incident_dossier", "inc-1") == changed


def test_in_memory_repository_rejects_lost_update_and_bad_identity() -> None:
    repo = InMemoryOperationalRepository()
    repo.upsert("risk_pattern", "risk-1", {"events": 1})
    with pytest.raises(OperationalVersionConflict):
        repo.upsert("risk_pattern", "risk-1", {"events": 2}, expected_version=0)
    with pytest.raises(ValueError):
        repo.upsert("unknown", "x", {})  # type: ignore[arg-type]


def test_load_is_partitioned_and_stably_sorted() -> None:
    repo = InMemoryOperationalRepository()
    repo.upsert("network_feature", "z", {"state": "candidate"})
    repo.upsert("incident_dossier", "b", {"state": "open"})
    repo.upsert("incident_dossier", "a", {"state": "open"})
    assert [row.record_id for row in repo.load("incident_dossier")] == ["a", "b"]


def test_postgres_schema_declares_all_business_objects_and_append_only_events() -> None:
    sql = PostgresOperationalRepository.schema_sql()
    assert "incident_dossier" in sql
    assert "risk_pattern" in sql
    assert "network_feature" in sql
    assert "operational_memory_events is append-only" in sql
