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
    incident_disposition_ledger_path: Path
    investigation_case_store_path: Path
    investigation_session_store_dir: Path
    knowledge_corpus_path: Path | None
    # Autopoiesis-owned alert and investigation feed landed by the event pipeline.
    stream_output_dir: Path

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
                "/data/autopoiesis-production/network-rca-trace.jsonl",
            )
        ).resolve()
        incident_disposition_ledger_path = Path(
            autopoiesis_env(
                "INCIDENT_DISPOSITION_LEDGER_PATH",
                "/data/autopoiesis-production/incidents/disposition.jsonl",
            )
        ).resolve()
        investigation_case_store_path = Path(
            autopoiesis_env(
                "INVESTIGATION_CASE_STORE_PATH",
                "/data/autopoiesis-production/investigations/cases.sqlite3",
            )
        ).resolve()
        investigation_session_store_dir = Path(
            autopoiesis_env(
                "INVESTIGATION_SESSION_STORE_DIR",
                "/data/autopoiesis-production/investigations/sessions",
            )
        ).resolve()
        knowledge_corpus_value = autopoiesis_env("KNOWLEDGE_CORPUS_PATH")
        knowledge_corpus_path = (
            Path(knowledge_corpus_value).resolve()
            if knowledge_corpus_value
            else None
        )
        stream_output_dir = Path(
            autopoiesis_env("STREAM_DIR", "/data/autopoiesis-production/stream")
        ).resolve()
        return cls(
            repo_root=repo_root,
            frontend_dist=frontend_dist,
            cors_origins=cors_origins,
            trace_ledger_path=trace_ledger_path,
            incident_disposition_ledger_path=incident_disposition_ledger_path,
            investigation_case_store_path=investigation_case_store_path,
            investigation_session_store_dir=investigation_session_store_dir,
            knowledge_corpus_path=knowledge_corpus_path,
            stream_output_dir=stream_output_dir,
        )
