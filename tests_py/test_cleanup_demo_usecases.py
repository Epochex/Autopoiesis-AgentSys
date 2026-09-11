from __future__ import annotations

import json
import sqlite3

from scripts.cleanup_demo_usecases import (
    cleanup_cases,
    cleanup_changes,
    collect_artifacts,
    read_jsonl,
    rewrite_lines_with_identifiers,
    rewrite_jsonl,
)


def test_collects_only_exact_controlled_usecase_artifacts():
    rows = [
        {
            "dataClassification": "controlled_test",
            "scenario": "memory_trajectory",
            "runId": "memory-1",
            "subject": "autopoiesis-acceptance-memory-a.service",
            "caseId": "case-warm",
            "baselineCaseId": "case-cold",
            "sessionId": "warm1",
            "baselineSessionId": "cold1",
            "metrics": {
                "memoryIds": ["proc-demo"],
                "baselineProcedureMemoryId": "proc-demo",
            },
        },
        {
            "dataClassification": "observed",
            "scenario": "memory_trajectory",
            "runId": "production-1",
            "caseId": "case-production",
        },
    ]

    artifacts = collect_artifacts(rows)

    assert artifacts.run_ids == {"memory-1"}
    assert artifacts.case_ids == {"case-cold", "case-warm"}
    assert artifacts.session_ids == {"cold1", "warm1"}
    assert artifacts.memory_ids == {"proc-demo"}
    assert "production-1" not in artifacts.identifiers


def test_jsonl_cleanup_preserves_unrelated_and_corrupt_rows(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"run_id": "remove-me"}) + "\n"
        + "partial-json\n"
        + json.dumps({"run_id": "keep-me"}) + "\n",
        encoding="utf-8",
    )

    assert rewrite_jsonl(path, lambda row: row.get("run_id") == "remove-me", apply=False) == 1
    assert len(read_jsonl(path)) == 2
    assert rewrite_jsonl(path, lambda row: row.get("run_id") == "remove-me", apply=True) == 1
    assert "remove-me" not in path.read_text(encoding="utf-8")
    assert "partial-json" in path.read_text(encoding="utf-8")
    assert "keep-me" in path.read_text(encoding="utf-8")


def test_large_ledger_filter_uses_exact_identifiers_and_preserves_other_lines(tmp_path):
    path = tmp_path / "large-trace.jsonl"
    path.write_text(
        '{"run":"demo-one"}\npartial-json\n{"run":"production-one"}\n',
        encoding="utf-8",
    )
    assert rewrite_lines_with_identifiers(path, {"demo-one"}, apply=False) == 1
    assert rewrite_lines_with_identifiers(path, {"demo-one"}, apply=True) == 1
    assert path.read_text(encoding="utf-8") == 'partial-json\n{"run":"production-one"}\n'


def test_sqlite_cleanup_deletes_only_selected_change_and_case(tmp_path):
    changes = tmp_path / "changes.sqlite3"
    with sqlite3.connect(changes) as db:
        db.executescript("""
            CREATE TABLE changes(id TEXT PRIMARY KEY, status TEXT, payload TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY, change_id TEXT, at REAL, kind TEXT, payload TEXT);
            CREATE TABLE leases(device TEXT PRIMARY KEY, change_id TEXT);
            CREATE TABLE rule_uses(rule_id TEXT, change_id TEXT UNIQUE, at REAL);
        """)
        for change_id in ("demo-change", "production-change"):
            db.execute("INSERT INTO changes VALUES (?, 'done', '{}')", (change_id,))
            db.execute("INSERT INTO events(change_id,at,kind,payload) VALUES (?,0,'done','{}')", (change_id,))
    result = cleanup_changes(changes, {"demo-change"}, apply=True)
    assert result == {"changes": 1, "change_events": 1}
    with sqlite3.connect(changes) as db:
        assert db.execute("SELECT id FROM changes").fetchall() == [("production-change",)]

    cases = tmp_path / "cases.sqlite3"
    with sqlite3.connect(cases) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE investigation_cases(case_id TEXT PRIMARY KEY);
            CREATE TABLE investigation_case_sources(
                source_kind TEXT, source_id TEXT, case_id TEXT REFERENCES investigation_cases(case_id) ON DELETE CASCADE
            );
        """)
        for case_id in ("case-demo", "case-production"):
            db.execute("INSERT INTO investigation_cases VALUES (?)", (case_id,))
            db.execute("INSERT INTO investigation_case_sources VALUES ('source', ?, ?)", (case_id, case_id))
    result = cleanup_cases(cases, {"case-demo"}, apply=True)
    assert result == {"cases": 1, "case_sources": 1}
    with sqlite3.connect(cases) as db:
        assert db.execute("SELECT case_id FROM investigation_cases").fetchall() == [("case-production",)]
