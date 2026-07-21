"""Ordered projection of durable memory events into online retrieval indexes.

PostgreSQL owns the memory snapshot and append-only event log.  This consumer
turns that log into the rebuildable in-process view used by BM25, exact asset
lookup and the optional HNSW/Flat vector lifecycle.  Its checkpoint is advanced
only after an event has reached every enabled index.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from core.memory.store import TieredMemoryStore


class MemoryEventSource(Protocol):
    def get_checkpoint(self, index_name: str) -> int: ...

    def read_events(self, *, after_offset: int = 0, limit: int = 1_000) -> list[Any]: ...

    def advance_checkpoint(self, index_name: str, event_offset: int) -> int: ...


class IndexProjectionError(RuntimeError):
    """An event did not reach every enabled retrieval projection."""

    def __init__(self, event_offset: int, cause: Exception) -> None:
        super().__init__(
            f"memory index projection failed at event_offset={event_offset}: "
            f"{type(cause).__name__}: {cause}"
        )
        self.event_offset = event_offset
        self.cause = cause


@dataclass
class ProjectionStats:
    batches: int = 0
    events_read: int = 0
    events_applied: int = 0
    replayed: int = 0
    failures: int = 0
    checkpoint: int = 0
    last_failed_offset: int | None = None
    last_error: str | None = None


class MemoryIndexProjector:
    """Consume one PostgreSQL memory stream in strictly increasing offset order.

    Calls are serialised, so a request-boundary sync and an administrative catch
    up cannot race.  On a partial-process crash the durable checkpoint remains
    behind; replay is safe because :meth:`TieredMemoryStore.apply_index_event`
    rejects offsets already installed in this process.
    """

    def __init__(
        self,
        repository: MemoryEventSource,
        memory: TieredMemoryStore,
        *,
        index_name: str = "memory-online-hybrid-v1",
        batch_size: int = 1_000,
    ) -> None:
        if not index_name.strip():
            raise ValueError("index_name must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.repository = repository
        self.memory = memory
        self.index_name = index_name
        self.batch_size = int(batch_size)
        self._lock = threading.Lock()
        checkpoint = repository.get_checkpoint(index_name)
        if checkpoint > 0 and not memory.records():
            raise ValueError(
                "a non-zero checkpoint requires a matching memory/index snapshot"
            )
        if memory.projected_offset < checkpoint:
            memory.prime_projection(checkpoint)
        self._stats = ProjectionStats(checkpoint=checkpoint)

    def stats(self) -> dict[str, int | str | None]:
        with self._lock:
            return asdict(self._stats)

    def sync_once(self) -> int:
        """Project at most one batch and return the number of consumed events."""
        with self._lock:
            checkpoint = self.repository.get_checkpoint(self.index_name)
            events = self.repository.read_events(
                after_offset=checkpoint,
                limit=self.batch_size,
            )
            self._stats.batches += 1
            self._stats.events_read += len(events)
            previous = checkpoint
            consumed = 0
            for event in events:
                offset = int(event.event_offset)
                if offset <= previous:
                    error = ValueError(
                        f"event offsets must increase: {offset} after {previous}"
                    )
                    self._record_failure(offset, error)
                    raise IndexProjectionError(offset, error) from error
                try:
                    applied = self.memory.apply_index_event(
                        event.record,
                        event_type=event.event_type,
                        event_offset=offset,
                        version=int(event.version),
                    )
                    # Checkpointing is deliberately after the full hybrid write.
                    advanced = self.repository.advance_checkpoint(self.index_name, offset)
                    if int(advanced) != offset:
                        raise RuntimeError(
                            f"checkpoint advanced to {advanced}, expected {offset}"
                        )
                except Exception as exc:
                    self._record_failure(offset, exc)
                    raise IndexProjectionError(offset, exc) from exc

                consumed += 1
                previous = offset
                self._stats.checkpoint = offset
                self._stats.events_applied += int(applied)
                self._stats.replayed += int(not applied)
                self._stats.last_failed_offset = None
                self._stats.last_error = None
            return consumed

    def sync_pending(self, *, max_batches: int | None = None) -> int:
        """Catch up until a short batch is observed or ``max_batches`` is hit."""
        if max_batches is not None and max_batches <= 0:
            raise ValueError("max_batches must be positive")
        total = 0
        batches = 0
        while max_batches is None or batches < max_batches:
            consumed = self.sync_once()
            total += consumed
            batches += 1
            if consumed < self.batch_size:
                break
        return total

    def has_pending(self) -> bool:
        """Return whether the durable log still contains an unprojected event."""
        with self._lock:
            checkpoint = self.repository.get_checkpoint(self.index_name)
            return bool(
                self.repository.read_events(after_offset=checkpoint, limit=1)
            )

    def _record_failure(self, offset: int, error: Exception) -> None:
        self._stats.failures += 1
        self._stats.last_failed_offset = offset
        self._stats.last_error = f"{type(error).__name__}: {error}"
