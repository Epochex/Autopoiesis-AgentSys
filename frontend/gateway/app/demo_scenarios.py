"""Durable receipts for the two operator-triggered defence demonstrations.

The scripts append progress and final receipts from real detector, action,
probe and memory paths. Controlled-demo records stay separate from the
production case counters while remaining directly reachable from the console.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


SCENARIOS = frozenset({"self_healing", "memory_trajectory"})
RUN_STATES = frozenset({"running", "passed", "failed"})


def runs_path() -> Path:
    configured = os.getenv("AUTOPOIESIS_DEMO_RUNS_PATH")
    if configured:
        return Path(configured).resolve()
    if os.getenv("AUTOPOIESIS_TEST_TMP"):
        return Path(os.environ["AUTOPOIESIS_TEST_TMP"]) / "demo-runs.jsonl"
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(tempfile.gettempdir()) / "autopoiesis-demo-runs-test.jsonl"
    return Path("/data/autopoiesis-production/demo-runs.jsonl")


def append_run(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append one progress or final receipt as a single locked JSONL record."""
    record = dict(payload)
    scenario = str(record.get("scenario") or "")
    status = str(record.get("status") or "")
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown demo scenario: {scenario!r}")
    if status not in RUN_STATES:
        raise ValueError(f"demo status must be one of {sorted(RUN_STATES)}")
    for key in ("runId", "startedAt", "completedAt", "subject", "headline"):
        if not str(record.get(key) or "").strip():
            raise ValueError(f"demo record requires {key}")
    record["dataClassification"] = "controlled_test"
    record.setdefault("metrics", {})
    record.setdefault("steps", [])

    path = runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Return valid records newest first; a partial final line is ignored."""
    path = runs_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        lines = handle.readlines()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("scenario") in SCENARIOS
            and row.get("status") in RUN_STATES
            and row.get("dataClassification") == "controlled_test"
        ):
            rows.append(row)
        if len(rows) >= max(1, limit):
            break
    return rows


def scenario_snapshot(limit: int = 20) -> dict[str, Any]:
    runs = recent_runs(limit)
    latest: dict[str, dict[str, Any]] = {}
    for row in runs:
        latest.setdefault(str(row["scenario"]), row)
    return {
        "ok": True,
        "dataClassification": "controlled_test",
        "runs": runs,
        "latest": latest,
    }
