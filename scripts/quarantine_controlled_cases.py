#!/usr/bin/env python3
"""Move acceptance-shaped cases out of the production projection.

The script first creates a complete SQLite backup, then deletes only cases whose
stable subject identity belongs to a controlled run. Foreign-key cascade removes
their source index rows. Re-running it is safe.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PREFIXES = (
    "autopoiesis-acceptance-", "bvaccept-", "controlled-", "managed-host-",
    "redpanda-e2e-", "replay-", "synthetic-",
)


def _controlled_text(value: str) -> bool:
    normalized = value.casefold()
    return any(prefix in normalized for prefix in PREFIXES)


def quarantine_sessions(sessions_dir: Path, backup_dir: Path) -> int:
    destination = backup_dir / "sessions"
    moved = 0
    try:
        candidates = list(sessions_dir.glob("*.json"))
    except OSError:
        return 0
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not _controlled_text(json.dumps(payload, ensure_ascii=False)):
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / path.name
        if target.exists():
            target = destination / f"{path.stem}-{datetime.now(timezone.utc).timestamp():.0f}.json"
        os.replace(path, target)
        moved += 1
    return moved


def quarantine(
    source: Path,
    backup_dir: Path,
    sessions_dir: Path | None = None,
) -> tuple[Path, int, int]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"cases-before-controlled-quarantine-{stamp}.sqlite3"
    source_conn = sqlite3.connect(source)
    source_conn.execute("PRAGMA foreign_keys=ON")
    backup_conn = sqlite3.connect(backup)
    try:
        source_conn.backup(backup_conn)
        backup_conn.commit()
    finally:
        backup_conn.close()
    patterns = tuple(f"{prefix}%" for prefix in PREFIXES)
    with source_conn:
        rows = source_conn.execute(
            f"SELECT case_id FROM investigation_cases WHERE "
            + " OR ".join("lower(subject) LIKE ?" for _ in PREFIXES),
            patterns,
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        if ids:
            source_conn.execute(
                f"DELETE FROM investigation_cases WHERE case_id IN ({','.join('?' for _ in ids)})",
                ids,
            )
    source_conn.close()
    moved_sessions = quarantine_sessions(sessions_dir, backup_dir) if sessions_dir else 0
    return backup, len(ids), moved_sessions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path,
        default=Path("/data/autopoiesis-production/investigations/cases.sqlite3"),
    )
    parser.add_argument(
        "--backup-dir", type=Path,
        default=Path("/data/autopoiesis-production/quarantine"),
    )
    parser.add_argument(
        "--sessions-dir", type=Path,
        default=Path("/data/autopoiesis-production/investigations/sessions"),
    )
    args = parser.parse_args()
    backup, removed, moved_sessions = quarantine(
        args.source, args.backup_dir, args.sessions_dir,
    )
    print(f"backup={backup}")
    print(f"controlled_cases_removed={removed}")
    print(f"controlled_sessions_moved={moved_sessions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
