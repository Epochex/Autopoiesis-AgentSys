"""Background maintenance for versioned retrieval indexes.

The worker deliberately knows nothing about FAISS or BM25.  It polls a cheap
policy predicate and invokes one compaction at a time.  Failures remain visible
through counters and the last error instead of terminating the serving process.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass
class MaintenanceStats:
    checks: int = 0
    attempts: int = 0
    completed: int = 0
    skipped: int = 0
    aborted: int = 0
    failures: int = 0
    running: bool = False
    last_duration_seconds: float = 0.0
    last_error: str | None = None


class IndexMaintenanceWorker:
    """Run threshold-triggered compaction outside request handling threads."""

    def __init__(
        self,
        should_compact: Callable[[], bool],
        compact: Callable[[], object],
        *,
        check_interval_seconds: float = 60.0,
        name: str = "index-maintenance",
    ) -> None:
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds must be positive")
        self._should_compact = should_compact
        self._compact = compact
        self._interval = float(check_interval_seconds)
        self._name = name
        self._stats = MaintenanceStats()
        self._stats_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def stats(self) -> dict[str, int | float | bool | str | None]:
        with self._stats_lock:
            return asdict(self._stats)

    def run_once(self) -> bool:
        """Perform one policy check; return whether compaction completed."""
        if not self._run_lock.acquire(blocking=False):
            with self._stats_lock:
                self._stats.skipped += 1
            return False
        started = time.perf_counter()
        try:
            with self._stats_lock:
                self._stats.checks += 1
            if not self._should_compact():
                with self._stats_lock:
                    self._stats.skipped += 1
                    self._stats.last_error = None
                return False
            with self._stats_lock:
                self._stats.attempts += 1
                self._stats.running = True
            result = self._compact()
            # Sparse compaction returns False when a concurrent write invalidates
            # its candidate. Vector compaction returns the installed generation.
            if result is False:
                with self._stats_lock:
                    self._stats.aborted += 1
                return False
            with self._stats_lock:
                self._stats.completed += 1
                self._stats.last_error = None
            return True
        except Exception as exc:  # serving must survive maintenance failures
            with self._stats_lock:
                self._stats.failures += 1
                self._stats.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            with self._stats_lock:
                self._stats.running = False
                self._stats.last_duration_seconds = time.perf_counter() - started
            self._run_lock.release()

    def start(self) -> None:
        """Start one daemon thread; repeated calls are idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def trigger(self) -> None:
        """Request an early check without running compaction on the caller."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop and join the worker; return whether it exited before timeout."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.run_once()

    def __enter__(self) -> "IndexMaintenanceWorker":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()
