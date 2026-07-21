from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from core.observability.schema import NodeObservationEvent


class ObservationLedger:
    """Append-only node event store with process-safe writes and replay."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: NodeObservationEvent) -> None:
        payload = (
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            # The business trace already fsyncs run/decision boundaries. This
            # derived node stream deliberately avoids a second synchronous disk
            # barrier, otherwise instrumentation would distort the latency it is
            # supposed to measure. Each complete line remains replayable; an
            # abrupt host-power loss may lose only the OS-buffered tail.
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def extend(self, events: Iterable[NodeObservationEvent]) -> None:
        for event in events:
            self.append(event)

    def replay(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
    ) -> list[NodeObservationEvent]:
        try:
            handle = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return []
        events: list[NodeObservationEvent] = []
        with handle:
            # Writers hold LOCK_EX until one complete JSONL record is appended.
            # LOCK_SH prevents a reader from interpreting an in-flight tail as
            # corruption while still allowing concurrent dashboard readers.
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = NodeObservationEvent.model_validate_json(line)
                    except ValidationError as exc:
                        raise ValueError(
                            f"corrupt observation event at {self.path}:{line_number}"
                        ) from exc
                    if trace_id is not None and event.trace_id != trace_id:
                        continue
                    if session_id is not None and event.session_id != session_id:
                        continue
                    events.append(event)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return events

    def trace_ids(self, *, limit: int = 100, session_id: str | None = None) -> list[str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        latest: dict[str, object] = {}
        for event in self.replay(session_id=session_id):
            latest[event.trace_id] = event.timestamp
        return [
            trace_id
            for trace_id, _ in sorted(
                latest.items(), key=lambda item: item[1], reverse=True
            )[:limit]
        ]
