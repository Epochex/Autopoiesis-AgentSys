from __future__ import annotations

import os
from pathlib import Path


PRODUCTION_ROOT = Path("/data/autopoiesis-runtime")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_all_known_production_write_paths_resolve_under_session_tmp(
    isolate_production_writes: Path,
):
    from core.llm import cost
    from core.remediate import sentinel
    from frontend.gateway.app import model_access, remediation
    from frontend.gateway.app.config import Settings

    settings = Settings.from_env()
    resolved = {
        "cost ledger": cost._ledger_path(),
        "remediation history": remediation._runs_path(),
        "sentinel timeline": sentinel._default_timeline(),
        "trace ledger": settings.trace_ledger_path,
        "incident disposition ledger": settings.incident_disposition_ledger_path,
        "LLM cache": model_access.CACHE_DIR,
        "ARP snapshot": Path(os.environ["AUTOPOIESIS_ARP_SNAPSHOT_PATH"]),
        "L2 identity ledger": Path(os.environ["AUTOPOIESIS_L2_LEDGER_PATH"]),
    }

    escaped = {
        name: path
        for name, path in resolved.items()
        if not _under(path, isolate_production_writes)
    }
    assert escaped == {}
    assert all(not _under(path, PRODUCTION_ROOT) for path in resolved.values())
    assert os.environ["AUTOPOIESIS_MEMORY_DSN"] == ""


def test_representative_writes_leave_production_files_unchanged(
    isolate_production_writes: Path,
):
    from core.llm import cost
    from frontend.gateway.app import remediation

    before = {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in PRODUCTION_ROOT.rglob("*")
        if path.is_file()
    }
    cost.record("test-model", "production-write-guard", None)
    remediation._append_run({"action": "test", "synthetic": True})
    after = {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in PRODUCTION_ROOT.rglob("*")
        if path.is_file()
    }

    assert after == before
    assert (isolate_production_writes / "llm-cost.jsonl").exists()
    assert (isolate_production_writes / "remediation-runs.jsonl").exists()
