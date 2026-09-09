#!/usr/bin/env python3
"""Compact legacy standalone sentinel noise while preserving useful evidence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


POLLUTED_SUBJECTS = {"fwupd-refresh.service", "udisks2.service"}


def is_polluted_subject(row: dict[str, Any]) -> bool:
    subject = str(row.get("subject") or "")
    return subject in POLLUTED_SUBJECTS or subject.startswith("bvaccept-")


def compact_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    materialized = list(rows)
    latest_failure: dict[tuple[str, str], int] = {}
    for index, row in enumerate(materialized):
        if row.get("kind") == "detector_failed":
            key = (str(row.get("detector") or ""), str(row.get("error") or ""))
            latest_failure[key] = index

    kept: list[dict[str, Any]] = []
    removed: Counter[str] = Counter()
    for index, row in enumerate(materialized):
        kind = str(row.get("kind") or "unknown")
        if is_polluted_subject(row):
            removed[f"polluted_subject:{kind}"] += 1
            continue
        if kind == "cycle" and int(row.get("acted") or 0) == 0:
            removed["no_action_cycle"] += 1
            continue
        if kind == "detector_failed":
            key = (str(row.get("detector") or ""), str(row.get("error") or ""))
            if latest_failure.get(key) != index:
                removed["duplicate_detector_failure"] += 1
                continue
        kept.append(row)
    return kept, removed


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON at line {line_number}: {error}") from error
            if not isinstance(row, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            rows.append(row)
    return rows


def atomic_write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path("/data/autopoiesis-production/sentinel-timeline.jsonl"),
    )
    parser.add_argument("--apply", action="store_true", help="atomically replace the source file")
    args = parser.parse_args()
    rows = load_rows(args.path)
    kept, removed = compact_rows(rows)
    if args.apply:
        atomic_write(args.path, kept)
    print(json.dumps({
        "applied": args.apply,
        "before": len(rows),
        "after": len(kept),
        "removed": dict(sorted(removed.items())),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
