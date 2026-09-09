#!/usr/bin/env python3
"""Remove controlled acceptance and replay cases from the production case store."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


CONTROLLED_PREFIXES = (
    "autopoiesis-acceptance-",
    "bvaccept-",
    "controlled-",
    "managed-host-",
    "replay-",
    "synthetic-",
)


def is_controlled(subject: str, payload: dict[str, Any]) -> bool:
    identities = (
        subject,
        payload.get("device"),
        payload.get("deviceKey"),
        dict(payload.get("incidentFacts") or {}).get("managedDevice"),
    )
    return any(
        str(identity or "").strip().casefold().startswith(CONTROLLED_PREFIXES)
        for identity in identities
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("/data/autopoiesis-production/investigations/cases.sqlite3"),
    )
    parser.add_argument(
        "--sessions",
        type=Path,
        default=Path("/data/autopoiesis-production/investigations/sessions"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.execute("PRAGMA foreign_keys=ON")
    selected: list[str] = []
    for case_id, subject, raw_payload in connection.execute(
        "SELECT case_id, subject, source_payload_json FROM investigation_cases"
    ):
        payload = json.loads(raw_payload)
        if is_controlled(str(subject), payload):
            selected.append(str(case_id))

    selected_set = set(selected)
    session_files: list[Path] = []
    for path in args.sessions.glob("*.json"):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(snapshot.get("case_id") or "") in selected_set:
            session_files.append(path)

    if args.apply and selected:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "DELETE FROM investigation_case_sources WHERE case_id=?",
            ((case_id,) for case_id in selected),
        )
        connection.executemany(
            "DELETE FROM investigation_cases WHERE case_id=?",
            ((case_id,) for case_id in selected),
        )
        connection.commit()
        for path in session_files:
            path.unlink()
    connection.close()
    print(json.dumps({
        "applied": args.apply,
        "controlledCases": len(selected),
        "controlledSessions": len(session_files),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
