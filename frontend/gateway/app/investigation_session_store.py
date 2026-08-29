"""Crash-safe snapshots for long-running interactive investigations."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


class InvestigationSessionStore:
    """Persist one bounded JSON snapshot per session with atomic replacement."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_id(session_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ValueError("invalid investigation session id")
        return session_id

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{self._validate_id(session_id)}.json"

    def save(self, snapshot: Mapping[str, Any]) -> Path:
        session_id = self._validate_id(str(snapshot.get("session_id") or ""))
        target = self.path_for(session_id)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{session_id}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(snapshot), handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("session_id") != session_id:
            raise ValueError("invalid investigation session snapshot")
        return payload

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        paths = sorted(
            self.root.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:limit]
        snapshots: list[dict[str, Any]] = []
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("session_id"):
                snapshots.append(payload)
        return snapshots


__all__ = ["InvestigationSessionStore"]
