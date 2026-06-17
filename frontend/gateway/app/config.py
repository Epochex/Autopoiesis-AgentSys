from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _default_repo_root() -> Path:
  return Path(__file__).resolve().parents[3]


def _split_csv(value: str) -> tuple[str, ...]:
  items = [item.strip() for item in value.split(',')]
  return tuple(item for item in items if item)


@dataclass(frozen=True)
class Settings:
  repo_root: Path
  artifact_root: Path
  frontend_dist: Path
  stream_interval_sec: float
  cors_origins: tuple[str, ...]
  branch_hint: str | None

  @classmethod
  def from_env(cls) -> 'Settings':
    repo_root = Path(
      os.getenv('CONTEXTHELIX_REPO_ROOT', os.getenv('NETOPS_CONSOLE_REPO_ROOT', str(_default_repo_root()))),
    ).resolve()
    frontend_dist = Path(
      os.getenv(
        'CONTEXTHELIX_FRONTEND_DIST',
        os.getenv('NETOPS_CONSOLE_FRONTEND_DIST', str(repo_root / 'frontend' / 'dist')),
      ),
    ).resolve()
    artifact_root = Path(
      os.getenv('CONTEXTHELIX_ARTIFACT_ROOT', os.getenv('NETOPS_RUNTIME_ROOT', '/data/netops-runtime')),
    ).resolve()
    stream_interval_sec = float(
      os.getenv('CONTEXTHELIX_STREAM_INTERVAL_SEC', os.getenv('NETOPS_CONSOLE_STREAM_INTERVAL_SEC', '1')),
    )
    cors_origins = _split_csv(os.getenv('CONTEXTHELIX_CORS_ORIGINS', os.getenv('NETOPS_CONSOLE_CORS_ORIGINS', '')))
    branch_hint = os.getenv('CONTEXTHELIX_BRANCH', os.getenv('NETOPS_CONSOLE_BRANCH'))

    return cls(
      repo_root=repo_root,
      artifact_root=artifact_root,
      frontend_dist=frontend_dist,
      stream_interval_sec=stream_interval_sec,
      cors_origins=cors_origins,
      branch_hint=branch_hint,
    )
