from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


_PRODUCTION_ROOT = Path("/data/autopoiesis-runtime")
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="autopoiesis-pytest-"))
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)
_POLLUTED_FILES = (
    _PRODUCTION_ROOT / "llm-cost.jsonl",
    _PRODUCTION_ROOT / "remediation-runs.jsonl",
)

# conftest is imported before pytest collects test modules. A fixture alone is
# too late: application modules imported during collection can freeze their
# path constants before PYTEST_CURRENT_TEST exists.
_PATH_ENV = {
    "AUTOPOIESIS_COST_LEDGER": _TEST_ROOT / "llm-cost.jsonl",
    "AUTOPOIESIS_REMEDIATION_LOG": _TEST_ROOT / "remediation-runs.jsonl",
    "AUTOPOIESIS_SENTINEL_TIMELINE": _TEST_ROOT / "sentinel-timeline.jsonl",
    "AUTOPOIESIS_TRACE_LEDGER_PATH": _TEST_ROOT / "network-rca-trace.jsonl",
    "AUTOPOIESIS_INCIDENT_DISPOSITION_LEDGER_PATH": _TEST_ROOT / "incidents" / "disposition.jsonl",
    "AUTOPOIESIS_LLM_CACHE_DIR": _TEST_ROOT / "llm-cache",
    "AUTOPOIESIS_ARP_SNAPSHOT_PATH": _TEST_ROOT / "arp-snapshot.txt",
    "AUTOPOIESIS_L2_LEDGER_PATH": _TEST_ROOT / "l2-identity-history.jsonl",
    "AUTOPOIESIS_REMEDIATION_STOP": _TEST_ROOT / "remediation-emergency-stop.json",
    "AUTOPOIESIS_REMEDIATION_BUDGET": _TEST_ROOT / "remediation-budget.json",
}


def _redirect_paths() -> None:
    os.environ["AUTOPOIESIS_TEST_TMP"] = str(_TEST_ROOT)
    os.environ["AUTOPOIESIS_MEMORY_DSN"] = ""
    for name, path in _PATH_ENV.items():
        os.environ[name] = str(path)
    # Production intentionally fails closed when the independent control file
    # is missing. Tests opt in explicitly and use high budgets so independent
    # cases do not consume each other's rolling window.
    os.environ["AUTOPOIESIS_REMEDIATION_MAX_PER_INCIDENT"] = "10000"
    os.environ["AUTOPOIESIS_REMEDIATION_MAX_PER_ASSET"] = "10000"
    os.environ["AUTOPOIESIS_REMEDIATION_MAX_PER_DOMAIN"] = "10000"
    os.environ["AUTOPOIESIS_REMEDIATION_MAX_CONCURRENCY"] = "10000"
    os.environ["AUTOPOIESIS_REMEDIATION_COOLDOWN"] = "0"
    os.environ["AUTOPOIESIS_REMEDIATION_BACKOFF_BASE"] = "0"
    os.environ["AUTOPOIESIS_REMEDIATION_BACKOFF_MAX"] = "0"
    control = _PATH_ENV["AUTOPOIESIS_REMEDIATION_STOP"]
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(
        json.dumps(
            {
                "paused": False,
                "reason": "pytest explicitly enabled remediation",
                "actor": "pytest",
                "timestamp": "2026-08-23T00:00:00Z",
                "fail_closed": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )


_redirect_paths()


def _is_under_production(path: object) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        Path(path).resolve().relative_to(_PRODUCTION_ROOT.resolve())
    except (OSError, TypeError, ValueError):
        return False
    return True


def _reject_production_writes(event: str, args: tuple[object, ...]) -> None:
    if event != "open" or not args or not _is_under_production(args[0]):
        return
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    mode_writes = isinstance(mode, str) and any(mark in mode for mark in "wax+")
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    flags_write = isinstance(flags, int) and bool(flags & write_flags)
    if mode_writes or flags_write:
        raise PermissionError(f"pytest attempted to write production path: {args[0]}")


# The audit hook covers the complete pytest process, including collection and
# fixture teardown. Directory stat comparisons alone cannot attribute writes:
# the deployed sentinel keeps appending from a different process during tests.
sys.addaudithook(_reject_production_writes)


def _polluted_file_state() -> dict[str, tuple[int, int]]:
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in _POLLUTED_FILES
        if path.exists()
    }


@pytest.fixture(scope="session", autouse=True)
def isolate_production_writes():
    """Keep every suite-owned sink in one temp tree and detect any escape."""
    _redirect_paths()
    before = _polluted_file_state()
    yield _TEST_ROOT
    after = _polluted_file_state()
    try:
        assert after == before, (
            "pytest changed a previously polluted production ledger; "
            f"before={before!r}, after={after!r}"
        )
    finally:
        # _TEST_ROOT comes directly from mkdtemp above, so cleanup cannot widen
        # to a caller-controlled or unresolved directory.
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)
