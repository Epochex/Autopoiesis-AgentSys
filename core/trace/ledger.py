from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from core.trace.events import TraceEvent


class JSONLTraceLedger:
    """Append-only JSONL trace ledger; one JSON object per line, replayable in order.

    Each `append` opens/closes the file so every event is durable immediately and
    the ledger stays valid even if the process dies mid-run.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: TraceEvent) -> None:
        """Durably append one event."""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")

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
