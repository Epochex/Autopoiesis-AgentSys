#!/usr/bin/env python3
"""Remove artifacts created by the two controlled acceptance use cases.

The target set is discovered only from durable ``controlled_test`` receipts.
Production cases, services, memories and traces are never selected by a caller
supplied pattern. Use ``--dry-run`` to inspect counts without changing data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCENARIOS = {"self_healing", "memory_trajectory"}
UNIT_PATTERN = re.compile(r"autopoiesis-acceptance-[A-Za-z0-9_-]+\.service")
DEFAULT_RUNS = Path("/data/autopoiesis-production/demo-runs.jsonl")
DEFAULT_CHANGES = Path("/data/autopoiesis-production/device-changes.sqlite3")
DEFAULT_SENTINEL = Path("/data/autopoiesis-production/sentinel-timeline.jsonl")


@dataclass
class Artifacts:
    run_ids: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    case_ids: set[str] = field(default_factory=set)
    session_ids: set[str] = field(default_factory=set)
    change_ids: set[str] = field(default_factory=set)
    memory_ids: set[str] = field(default_factory=set)

    @property
    def identifiers(self) -> set[str]:
        return {
            value
            for group in (
                self.run_ids,
                self.units,
                self.case_ids,
                self.session_ids,
                self.change_ids,
                self.memory_ids,
            )
            for value in group
            if value
        }


def _controlled_run(row: Any) -> bool:
    return bool(
        isinstance(row, dict)
        and row.get("dataClassification") == "controlled_test"
        and row.get("scenario") in SCENARIOS
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def collect_artifacts(rows: Iterable[dict[str, Any]]) -> Artifacts:
    found = Artifacts()
    for row in rows:
        if not _controlled_run(row):
            continue
        for key, target in (
            ("runId", found.run_ids),
            ("subject", found.units),
            ("caseId", found.case_ids),
            ("baselineCaseId", found.case_ids),
            ("comparisonCaseId", found.case_ids),
            ("sessionId", found.session_ids),
            ("baselineSessionId", found.session_ids),
            ("changeId", found.change_ids),
        ):
            value = str(row.get(key) or "").strip()
            if value:
                target.add(value)
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        memory_ids = list(metrics.get("memoryIds") or ())
        memory_ids.append(metrics.get("baselineProcedureMemoryId"))
        found.memory_ids.update(str(value) for value in memory_ids if value)
    found.units = {unit for unit in found.units if UNIT_PATTERN.fullmatch(unit)}
    found.session_ids = {
        value for value in found.session_ids if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
    }
    # Self-healing uses a synthetic case scope which is not present in the
    # investigation case database.
    found.case_ids = {value for value in found.case_ids if value.startswith("case-")}
    return found


def discover_from_stores(case_store: Path, sentinel_path: Path) -> Artifacts:
    """Find controlled-use-case data by the unit names it is about.

    The run receipt file is the primary index, but it is also the easiest thing
    to lose: an operator wipes it, a run is interrupted before its receipt is
    written, or an earlier cleanup already rewrote it. The cases, sessions and
    sentinel rows those runs produced are still there, all naming the
    ``autopoiesis-acceptance-*.service`` unit that only a controlled run ever
    creates, so they can be found from that name alone."""
    found = Artifacts()
    if case_store.exists():
        with sqlite3.connect(f"file:{case_store}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT case_id, subject, timeline_json, source_payload_json FROM investigation_cases "
                "WHERE subject LIKE 'autopoiesis-acceptance-%'"
            ).fetchall()
        for row in rows:
            if not UNIT_PATTERN.fullmatch(str(row["subject"] or "")):
                continue
            found.units.add(str(row["subject"]))
            found.case_ids.add(str(row["case_id"]))
            for blob in (row["timeline_json"], row["source_payload_json"]):
                try:
                    value = json.loads(blob or "null")
                except json.JSONDecodeError:
                    continue
                _harvest_identifiers(value, found)
    for row in read_jsonl(sentinel_path):
        subject = str(row.get("subject") or row.get("target") or "")
        if UNIT_PATTERN.fullmatch(subject):
            found.units.add(subject)
            _harvest_identifiers(row, found)
    found.session_ids = {
        value for value in found.session_ids if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
    }
    found.case_ids = {value for value in found.case_ids if value.startswith("case-")}
    return found


def _harvest_identifiers(value: Any, found: Artifacts) -> None:
    """Walk a record and collect every run, case, session, change and memory id it names."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and item.strip():
                text = item.strip()
                if key in ("runId", "demoRunId"):
                    found.run_ids.add(text)
                elif key in ("caseId", "baselineCaseId", "comparisonCaseId") and text.startswith("case-"):
                    found.case_ids.add(text)
                elif key in ("sessionId", "baselineSessionId", "session_id"):
                    found.session_ids.add(text)
                elif key in ("changeId", "change_id"):
                    found.change_ids.add(text)
                elif key in ("memoryId", "memory_id", "baselineProcedureMemoryId"):
                    found.memory_ids.add(text)
            else:
                _harvest_identifiers(item, found)
    elif isinstance(value, list):
        for item in value:
            _harvest_identifiers(item, found)


def _contains_identifier(row: Any, identifiers: set[str]) -> bool:
    if not identifiers:
        return False
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    return any(identifier in encoded for identifier in identifiers)


def rewrite_jsonl(
    path: Path,
    remove: Callable[[dict[str, Any]], bool],
    *,
    apply: bool,
    raw_remove: Callable[[str], bool] | None = None,
) -> int:
    if not path.exists():
        return 0
    removed = 0
    temporary: Path | None = None
    output = None
    if apply:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        output = os.fdopen(descriptor, "w", encoding="utf-8")
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if raw_remove is not None:
                    if raw_remove(line):
                        removed += 1
                    elif output is not None:
                        output.write(line)
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if output is not None:
                        output.write(line)
                    continue
                if isinstance(row, dict) and remove(row):
                    removed += 1
                    continue
                if output is not None:
                    output.write(line)
        if output is not None and temporary is not None:
            output.flush()
            os.fsync(output.fileno())
            output.close()
            output = None
            metadata = path.stat()
            os.chmod(temporary, metadata.st_mode & 0o777)
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
            os.replace(temporary, path)
            temporary = None
    finally:
        if output is not None:
            output.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return removed


def rewrite_lines_with_identifiers(
    path: Path,
    identifiers: set[str],
    *,
    apply: bool,
) -> int:
    """Filter a large append-only ledger with the system fixed-string scanner."""
    if not path.exists() or not identifiers:
        return 0
    pattern_fd, pattern_name = tempfile.mkstemp(prefix="autopoiesis-demo-identifiers-")
    pattern_path = Path(pattern_name)
    temporary: Path | None = None
    try:
        with os.fdopen(pattern_fd, "w", encoding="utf-8") as patterns:
            for value in sorted(identifiers):
                patterns.write(value + "\n")
        counted = subprocess.run(
            ["grep", "-a", "-F", "-c", "-f", str(pattern_path), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if counted.returncode not in {0, 1}:
            raise RuntimeError(f"failed to scan {path}: {counted.stderr.strip()}")
        removed = int((counted.stdout or "0").strip() or 0)
        if not apply:
            return removed

        output_fd, output_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(output_name)
        with os.fdopen(output_fd, "wb") as output:
            filtered = subprocess.run(
                ["grep", "-a", "-F", "-v", "-f", str(pattern_path), str(path)],
                check=False,
                stdout=output,
                stderr=subprocess.PIPE,
            )
            if filtered.returncode not in {0, 1}:
                raise RuntimeError(f"failed to rewrite {path}: {filtered.stderr.decode(errors='replace').strip()}")
            output.flush()
            os.fsync(output.fileno())
        metadata = path.stat()
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
        temporary = None
        return removed
    finally:
        pattern_path.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _delete_sqlite_rows(path: Path, table: str, column: str, values: set[str], *, apply: bool) -> int:
    if not path.exists() or not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    with sqlite3.connect(path) as connection:
        count = int(connection.execute(
            f"SELECT count(*) FROM {table} WHERE {column} IN ({placeholders})",
            tuple(sorted(values)),
        ).fetchone()[0])
        if apply and count:
            connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(sorted(values)),
            )
    return count


def cleanup_changes(path: Path, change_ids: set[str], *, apply: bool) -> dict[str, int]:
    if not path.exists() or not change_ids:
        return {"changes": 0, "change_events": 0}
    placeholders = ",".join("?" for _ in change_ids)
    values = tuple(sorted(change_ids))
    with sqlite3.connect(path) as connection:
        changes = int(connection.execute(
            f"SELECT count(*) FROM changes WHERE id IN ({placeholders})", values
        ).fetchone()[0])
        events = int(connection.execute(
            f"SELECT count(*) FROM events WHERE change_id IN ({placeholders})", values
        ).fetchone()[0])
        if apply:
            connection.execute(f"DELETE FROM events WHERE change_id IN ({placeholders})", values)
            connection.execute(f"DELETE FROM rule_uses WHERE change_id IN ({placeholders})", values)
            connection.execute(f"DELETE FROM leases WHERE change_id IN ({placeholders})", values)
            connection.execute(f"DELETE FROM changes WHERE id IN ({placeholders})", values)
            connection.commit()
            connection.execute("VACUUM")
    return {"changes": changes, "change_events": events}


def cleanup_cases(path: Path, case_ids: set[str], *, apply: bool) -> dict[str, int]:
    if not path.exists() or not case_ids:
        return {"cases": 0, "case_sources": 0}
    placeholders = ",".join("?" for _ in case_ids)
    values = tuple(sorted(case_ids))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        cases = int(connection.execute(
            f"SELECT count(*) FROM investigation_cases WHERE case_id IN ({placeholders})",
            values,
        ).fetchone()[0])
        sources = int(connection.execute(
            f"SELECT count(*) FROM investigation_case_sources WHERE case_id IN ({placeholders})",
            values,
        ).fetchone()[0])
        if apply:
            connection.execute(
                f"DELETE FROM investigation_cases WHERE case_id IN ({placeholders})", values
            )
            connection.commit()
            connection.execute("VACUUM")
    return {"cases": cases, "case_sources": sources}


def _load_service_environment() -> None:
    """Load only settings required by cleanup, without printing their values."""
    path = Path("/etc/autopoiesis.env")
    if not path.exists():
        return
    allowed = {
        "AUTOPOIESIS_MEMORY_DSN",
        "AUTOPOIESIS_MEMORY_SCHEMA",
        "AUTOPOIESIS_TRACE_LEDGER_PATH",
        "AUTOPOIESIS_INCIDENT_DISPOSITION_LEDGER_PATH",
        "AUTOPOIESIS_INVESTIGATION_CASE_STORE_PATH",
        "AUTOPOIESIS_INVESTIGATION_SESSION_STORE_DIR",
        "AUTOPOIESIS_CHANGE_DB",
        "AUTOPOIESIS_DEMO_RUNS_PATH",
        "AUTOPOIESIS_SENTINEL_TIMELINE",
    }
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowed and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def cleanup_memories(memory_ids: set[str], *, apply: bool) -> int:
    dsn = os.getenv("AUTOPOIESIS_MEMORY_DSN", "").strip()
    if not dsn or not memory_ids:
        return 0
    from core.memory.postgres_repository import PostgresMemoryRepository

    repository = PostgresMemoryRepository(
        dsn,
        schema=os.getenv("AUTOPOIESIS_MEMORY_SCHEMA", "autopoiesis_production"),
    )
    existing = [memory_id for memory_id in sorted(memory_ids) if repository.get(memory_id)]
    if apply:
        for memory_id in existing:
            repository.purge(
                memory_id,
                "controlled acceptance use case cleanup",
                actor="controlled-demo-cleanup",
                allow_purge=True,
            )
    return len(existing)


def cleanup_units(units: set[str], *, apply: bool) -> int:
    paths = {
        path
        for path in Path("/run/systemd/system").glob("autopoiesis-acceptance-*.service")
        if UNIT_PATTERN.fullmatch(path.name)
    }
    paths.update(Path("/run/systemd/system") / unit for unit in units)
    paths = {path for path in paths if path.exists() and UNIT_PATTERN.fullmatch(path.name)}
    if apply:
        for path in sorted(paths):
            subprocess.run(["systemctl", "stop", path.name], check=False, capture_output=True)
            subprocess.run(["systemctl", "reset-failed", path.name], check=False, capture_output=True)
            path.unlink(missing_ok=True)
            (Path("/run") / f"{path.name}.fail-once").unlink(missing_ok=True)
        Path("/run/autopoiesis-memory-demo.lock").unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
    return len(paths)


def wait_for_gateway(timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8026/api/healthz", timeout=2):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError("网关重启后 45 秒内未恢复")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只显示将删除的受控演示数据")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("需要 root 权限")
    apply = not args.dry_run
    _load_service_environment()

    from frontend.gateway.app.config import Settings

    settings = Settings.from_env()
    runs_path = Path(os.getenv("AUTOPOIESIS_DEMO_RUNS_PATH", str(DEFAULT_RUNS)))
    artifacts = collect_artifacts(read_jsonl(runs_path))
    # Whatever the receipt file no longer says, the stores still do.
    discovered = discover_from_stores(
        settings.investigation_case_store_path,
        Path(os.getenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(DEFAULT_SENTINEL))),
    )
    for group in ("run_ids", "units", "case_ids", "session_ids", "change_ids", "memory_ids"):
        getattr(artifacts, group).update(getattr(discovered, group))
    identifiers = artifacts.identifiers
    counts: dict[str, int] = {
        "runs": len(artifacts.run_ids),
        "units": cleanup_units(artifacts.units, apply=apply),
        "sessions": sum(
            settings.investigation_session_store_dir.joinpath(f"{session_id}.json").exists()
            for session_id in artifacts.session_ids
        ),
    }

    gateway_was_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "autopoiesis-gateway.service"],
        check=False,
    ).returncode == 0
    if apply and gateway_was_active:
        subprocess.run(["systemctl", "stop", "autopoiesis-gateway.service"], check=True)
    try:
        counts.update(cleanup_changes(
            Path(os.getenv("AUTOPOIESIS_CHANGE_DB", str(DEFAULT_CHANGES))),
            artifacts.change_ids,
            apply=apply,
        ))
        counts.update(cleanup_cases(
            settings.investigation_case_store_path,
            artifacts.case_ids,
            apply=apply,
        ))
        counts["memories"] = cleanup_memories(artifacts.memory_ids, apply=apply)

        trace_paths = {
            "sentinel_rows": Path(os.getenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(DEFAULT_SENTINEL))),
            "trace_rows": settings.trace_ledger_path,
            "observability_rows": settings.trace_ledger_path.with_name(
                f"{settings.trace_ledger_path.stem}.observability{settings.trace_ledger_path.suffix}"
            ),
            "disposition_rows": settings.incident_disposition_ledger_path,
        }
        for label, path in trace_paths.items():
            counts[label] = rewrite_lines_with_identifiers(
                path,
                identifiers,
                apply=apply,
            )
        counts["demo_receipt_rows"] = rewrite_jsonl(
            runs_path, _controlled_run, apply=apply
        )
        if apply:
            for session_id in artifacts.session_ids:
                settings.investigation_session_store_dir.joinpath(
                    f"{session_id}.json"
                ).unlink(missing_ok=True)
    finally:
        if apply and gateway_was_active:
            subprocess.run(["systemctl", "start", "autopoiesis-gateway.service"], check=True)
            wait_for_gateway()

    mode = "将清理" if args.dry_run else "已清理"
    print(f"{mode}两个受控用例的数据：")
    print(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True))
    if apply:
        print("临时服务、回执、案件、会话、精确记忆和关联轨迹已处理；生产数据未进入目标集合。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
