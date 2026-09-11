"""Crash-safe snapshots for long-running interactive investigations."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

# Matching-field summaries of session snapshots, per store directory. Shared by
# every store instance so a per-request store does not rebuild it (see summaries).
_SUMMARY_INDEXES: dict[Path, dict[Path, tuple[tuple[int, int], dict[str, Any]]]] = {}
_INDEX_LOCK = threading.Lock()


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

    def _snapshot_paths(self):
        # pathlib's glob matches dotfiles, and the summary index sidecar lives
        # in this directory: it is not a session and must never be read as one.
        return (path for path in self.root.glob("*.json") if not path.name.startswith("."))

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
        limit = max(1, min(int(limit), 5_000))
        paths = sorted(
            self._snapshot_paths(),
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

    # ── locating one session without reading them all ──────────────────────
    #
    # The live memory receipt is polled every 5 s and wants the newest session
    # for one subject or case. ``recent(1_000)`` answered that by parsing the
    # thousand newest snapshots in full — 378 MB of JSON on 10 Sep, evidence
    # bodies and prompts included — and throwing all but one away; with the
    # trace ledger replay this is what pushed the gateway past its memory limit.
    # Snapshots are replaced atomically and never edited in place, so a file's
    # (mtime, size) identifies its content: the few fields matching needs are
    # indexed once per file version, and only the chosen snapshot is loaded.
    _INDEX_FIELDS = ("session_id", "subject", "case_id", "opened_at",
                     "evaluation_source_case_id", "evaluation_only")

    def __init_index__(self) -> dict[Path, tuple[tuple[int, int], dict[str, Any]]]:
        # Callers build a fresh store per request (see investigate._session_store),
        # so the index lives at module level, keyed by the directory it describes.
        return _SUMMARY_INDEXES.setdefault(self.root.resolve(), {})

    def _summary(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("session_id"):
            return None
        summary = {field: payload.get(field) for field in self._INDEX_FIELDS}
        summary["source_ids"] = [
            str(source.get("source_id") or "")
            for source in payload.get("source_refs") or ()
            if isinstance(source, Mapping)
        ]
        return summary

    def summaries(self, limit: int = 1_000) -> list[dict[str, Any]]:
        """Matching fields of the newest ``limit`` snapshots, newest first.

        Parses only files whose (mtime, size) changed since the last call.
        """
        limit = max(1, min(int(limit), 5_000))
        with _INDEX_LOCK:
            return self._summaries_locked(limit)

    _SIDECAR = ".summary-index.json"

    def _sidecar_load(self, index: dict[Path, tuple[tuple[int, int], dict[str, Any]]]) -> None:
        """Seed the in-memory index from the sidecar written by an earlier build."""
        try:
            raw = json.loads((self.root / self._SIDECAR).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for name, entry in (raw.get("files") or {}).items() if isinstance(raw, dict) else ():
            if not isinstance(entry, dict) or not isinstance(entry.get("version"), list):
                continue
            version = entry["version"]
            if len(version) == 2 and isinstance(entry.get("summary"), dict):
                index[self.root / name] = ((int(version[0]), int(version[1])), entry["summary"])
        index["__sidecar_loaded__"] = ((0, 0), {})  # type: ignore[index]

    def _sidecar_save(self, index: dict[Path, tuple[tuple[int, int], dict[str, Any]]]) -> None:
        payload = {"files": {
            path.name: {"version": list(version), "summary": summary}
            for path, (version, summary) in index.items() if isinstance(path, Path)
        }}
        fd, temporary_name = tempfile.mkstemp(prefix=".summary-index.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(temporary_name, self.root / self._SIDECAR)
        except OSError:
            Path(temporary_name).unlink(missing_ok=True)

    def _summaries_locked(self, limit: int) -> list[dict[str, Any]]:
        index = self.__init_index__()
        if "__sidecar_loaded__" not in index:
            self._sidecar_load(index)
        stamped = []
        for path in self._snapshot_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            stamped.append((stat.st_mtime_ns, path, (stat.st_mtime_ns, stat.st_size)))
        stamped.sort(key=lambda item: item[0], reverse=True)
        newest = stamped[:limit]
        # Callers ask for different windows (the receipt reads the newest
        # thousand, the retrieval page all of them); the index must outlive the
        # smaller request, or the two evict each other's entries and re-parse
        # thousands of files on every alternate poll. Only files that are gone
        # leave the index.
        present = {path for _, path, _ in stamped}
        for stale in [path for path in index if isinstance(path, Path) and path not in present]:
            del index[stale]
        out: list[dict[str, Any]] = []
        parsed = 0
        for _, path, version in newest:
            cached = index.get(path)
            if cached is None or cached[0] != version:
                summary = self._summary(path)
                parsed += 1
                if summary is None:
                    index.pop(path, None)
                    continue
                index[path] = (version, summary)
                cached = index[path]
            out.append(cached[1])
        if parsed:
            self._sidecar_save(index)
        if parsed >= 50:
            # Parsing hundreds of multi-megabyte snapshots leaves freed arena
            # pages that glibc keeps; hand them back so the build is not paid
            # for in resident memory for the rest of the process's life.
            try:
                import ctypes
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except (OSError, AttributeError):
                pass
        return out

    def find_latest(
        self,
        *,
        subject: str,
        incident_ref: str | None = None,
        limit: int = 1_000,
        by_source_refs: bool = True,
    ) -> dict[str, Any] | None:
        """The newest live session for a case (by id or source ref) or subject.

        Same selection the memory receipt made over ``recent(limit)``, without
        loading any snapshot but the one returned.
        """
        summaries = [
            item for item in self.summaries(limit)
            if not item.get("evaluation_source_case_id") and not item.get("evaluation_only")
        ]
        if incident_ref:
            chosen = next((item for item in summaries
                           if str(item.get("case_id") or "") == incident_ref
                           or (by_source_refs and incident_ref in item.get("source_ids", ()))), None)
        else:
            chosen = next((item for item in summaries
                           if str(item.get("subject") or "") == subject), None)
        if chosen is None:
            return None
        try:
            return self.load(str(chosen["session_id"]))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def recent_trajectory(
        self,
        limit: int = 1_000,
        *,
        max_events_per_session: int = 500,
    ) -> list[dict[str, Any]]:
        """Load only fields used to train the read-only trajectory model.

        Investigation snapshots also contain evidence bodies, prompts and tool
        outputs. Retaining those for hundreds of sessions can exceed the
        gateway memory limit even though trajectory analysis only consumes an
        activity name and terminal outcome from each event.
        """
        limit = max(1, min(int(limit), 5_000))
        event_limit = max(1, min(int(max_events_per_session), 500))
        paths = sorted(
            self._snapshot_paths(),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:limit]
        projected: list[dict[str, Any]] = []
        activity_keys = ("kind", "event_kind", "eventKind", "type", "name", "action")
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not payload.get("session_id"):
                continue
            events = payload.get("trace_events")
            if not isinstance(events, list):
                events = payload.get("traceEvents")
            activities: list[Any] = []
            for event in list(events or ())[-event_limit:]:
                if not isinstance(event, dict):
                    activities.append(event)
                    continue
                activity = next(
                    (event.get(key) for key in activity_keys if event.get(key) is not None),
                    None,
                )
                if activity is None and isinstance(event.get("payload"), dict):
                    inner = event["payload"]
                    activity = next(
                        (inner.get(key) for key in activity_keys if inner.get(key) is not None),
                        None,
                    )
                if activity is not None:
                    activities.append(activity)
            projected.append({
                "session_id": payload.get("session_id"),
                "case_id": payload.get("case_id"),
                "trace_events": activities,
                "decision": payload.get("decision"),
                "resolution": payload.get("resolution"),
            })
        return projected


__all__ = ["InvestigationSessionStore"]
