from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    frontend_dist: Path
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        repo_root = Path(os.getenv("SELFEVO_REPO_ROOT", str(_default_repo_root()))).resolve()
        frontend_dist = Path(
            os.getenv("SELFEVO_FRONTEND_DIST", str(repo_root / "frontend" / "dist"))
        ).resolve()
        cors_origins = _split_csv(os.getenv("SELFEVO_CORS_ORIGINS", ""))
        return cls(repo_root=repo_root, frontend_dist=frontend_dist, cors_origins=cors_origins)
