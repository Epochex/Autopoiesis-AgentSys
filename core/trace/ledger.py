from __future__ import annotations

import json
import os
import fcntl
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from core.trace.events import TraceEvent


class JSONLTraceLedger:
    """Append-only JSONL trace ledger; one JSON object per line, replayable in order.

    Writers take an inter-process file lock so JSON records cannot interleave.
    ``fsync`` uses group-commit semantics at run/decision boundaries rather than
    forcing a disk flush for every observation; a completed diagnosis is durable
    together with every event written before it without destroying throughput.
    """

    _SYNC_KINDS = {
        "diagnosis_completed",
        "step_verified",
        "skill_promoted",
        "step_rolled_back",
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_sync_boundary(cls, kind: str) -> bool:
        """Return whether appending ``kind`` forces the current group to disk.

        Observability and diagnostics use this public contract instead of
        reaching into the ledger's implementation details.
        """
        return kind in cls._SYNC_KINDS

    def append(self, event: TraceEvent) -> None:
        """Durably append one event."""
        payload = (
            json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            if self.is_sync_boundary(event.kind):
                os.fsync(descriptor)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def extend(self, events: Iterable[TraceEvent]) -> None:
        """Append events in order."""
        for event in events:
            self.append(event)

    def replay(self) -> list[TraceEvent]:
        """Return every recorded event in append order; [] if the ledger does not exist.

        Raises ValueError naming the offending line if the file is corrupt —
        a trace that cannot be replayed must never be silently truncated.
        """
        if not self.path.exists():
            return []
        events: list[TraceEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(TraceEvent.model_validate_json(line))
                except ValidationError as exc:
                    raise ValueError(f"corrupt trace event at {self.path}:{line_number}") from exc
        return events
