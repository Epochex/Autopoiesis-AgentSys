from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def autopoiesis_env(suffix: str, default: str | None = None) -> str | None:
    primary = f"AUTOPOIESIS_{suffix}"
    legacy = f"SELFEVO_{suffix}"
    if primary in os.environ:
        return os.environ[primary]
    if legacy in os.environ:
        return os.environ[legacy]
    return default


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    frontend_dist: Path
    cors_origins: tuple[str, ...]
    trace_ledger_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        repo_root = Path(autopoiesis_env("REPO_ROOT", str(_default_repo_root()))).resolve()
        frontend_dist = Path(
            autopoiesis_env("FRONTEND_DIST", str(repo_root / "frontend" / "dist"))
        ).resolve()
        cors_origins = _split_csv(autopoiesis_env("CORS_ORIGINS", ""))
        trace_ledger_path = Path(
            autopoiesis_env(
                "TRACE_LEDGER_PATH",
                "/data/autopoiesis-runtime/network-rca-trace.jsonl",
            )
        ).resolve()
        return cls(
            repo_root=repo_root,
            frontend_dist=frontend_dist,
            cors_origins=cors_origins,
            trace_ledger_path=trace_ledger_path,
        )
