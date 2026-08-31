import sqlite3

from scripts.quarantine_controlled_cases import quarantine


def test_quarantine_backs_up_then_removes_only_controlled_subjects(tmp_path):
    source = tmp_path / "cases.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE investigation_cases (case_id TEXT PRIMARY KEY, subject TEXT NOT NULL);
            CREATE TABLE investigation_case_sources (
                source_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES investigation_cases(case_id) ON DELETE CASCADE
            );
            INSERT INTO investigation_cases VALUES ('real', '192.168.1.4');
            INSERT INTO investigation_cases VALUES ('test-a', 'bvaccept-fail-abc.service');
            INSERT INTO investigation_cases VALUES ('test-b', 'managed-host-abc');
            INSERT INTO investigation_case_sources VALUES ('s-real', 'real');
            INSERT INTO investigation_case_sources VALUES ('s-test', 'test-a');
            """
        )

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "real.json").write_text('{"subject":"192.168.1.4"}', encoding="utf-8")
    (sessions / "test.json").write_text('{"subject":"managed-host-abc"}', encoding="utf-8")

    backup, removed, moved_sessions = quarantine(source, tmp_path / "backup", sessions)

    assert removed == 2
    assert moved_sessions == 1
    assert backup.exists()
    assert (sessions / "real.json").exists()
    assert not (sessions / "test.json").exists()
    assert (tmp_path / "backup" / "sessions" / "test.json").exists()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT case_id FROM investigation_cases").fetchall() == [("real",)]
        assert connection.execute("SELECT source_id FROM investigation_case_sources").fetchall() == [("s-real",)]
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT count(*) FROM investigation_cases").fetchone()[0] == 3
