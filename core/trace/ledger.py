from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from core.trace.events import TraceEvent


class JSONLTraceLedger:
    """Append-only JSONL trace ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: TraceEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")

    def extend(self, events: Iterable[TraceEvent]) -> None:
        for event in events:
            self.append(event)

    def replay(self) -> list[TraceEvent]:
        if not self.path.exists():
            return []
        events: list[TraceEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(TraceEvent.model_validate_json(line))
        return events
