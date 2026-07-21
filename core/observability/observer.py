from __future__ import annotations

import contextvars
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.observability.ledger import ObservationLedger
from core.observability.schema import NodeObservationEvent, ObservationStatus


_ACTIVE_SPAN: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "autopoiesis_active_observation_span", default=None
)
_SECRET_KEY = re.compile(
    r"(^|_)(password|passwd|secret|api_?key|access_?token|refresh_?token|authorization)($|_)",
    re.IGNORECASE,
)


def summarize_value(
    value: Any,
    *,
    max_depth: int = 4,
    max_items: int = 24,
    max_string: int = 512,
    _depth: int = 0,
) -> Any:
    """Bound and redact arbitrary node inputs before they enter observability."""
    if _depth >= max_depth:
        return "[depth-limited]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return value[:max_string] + f"…[truncated {len(value) - max_string} chars]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:max_items]:
            name = str(key)
            result[name] = (
                "[redacted]"
                if _SECRET_KEY.search(name)
                else summarize_value(
                    item,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string=max_string,
                    _depth=_depth + 1,
                )
            )
        if len(items) > max_items:
            result["_truncated_items"] = len(items) - max_items
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            summarize_value(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
                _depth=_depth + 1,
            )
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            result.append(f"[truncated {len(items) - max_items} items]")
        return result
    return summarize_value(
        repr(value),
        max_depth=max_depth,
        max_items=max_items,
        max_string=max_string,
        _depth=_depth + 1,
    )


class ObservationSpan:
    def __init__(
        self,
        observer: "ExecutionObserver",
        *,
        trace_id: str,
        session_id: str | None,
        case_id: str,
        node_name: str,
        node_type: str,
        parent_span_id: str | None,
        input: dict[str, Any] | None,
        attributes: dict[str, Any] | None,
    ) -> None:
        self.observer = observer
        self.trace_id = trace_id
        self.session_id = session_id
        self.case_id = case_id
        self.node_name = node_name
        self.node_type = node_type
        self.span_id = uuid4().hex[:16]
        self.parent_span_id = parent_span_id
        self.input = summarize_value(input or {})
        self.output: dict[str, Any] = {}
        self.metrics: dict[str, int | float | bool] = {}
        self.attributes = summarize_value(attributes or {})
        self.status: ObservationStatus = "ok"
        self.error: str | None = None
        self._started_ns = 0
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "ObservationSpan":
        self._started_ns = time.perf_counter_ns()
        self.observer._append(
            NodeObservationEvent(
                trace_id=self.trace_id,
                session_id=self.session_id,
                case_id=self.case_id,
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
                node_name=self.node_name,
                node_type=self.node_type,
                phase="started",
                status="running",
                input=self.input,
                attributes=self.attributes,
            )
        )
        self._token = _ACTIVE_SPAN.set((self.trace_id, self.span_id))
        return self

    def set_result(
        self,
        *,
        output: dict[str, Any] | None = None,
        metrics: dict[str, int | float | bool] | None = None,
        attributes: dict[str, Any] | None = None,
        status: ObservationStatus | None = None,
    ) -> None:
        if output is not None:
            self.output.update(summarize_value(output))
        if metrics is not None:
            self.metrics.update(metrics)
        if attributes is not None:
            self.attributes.update(summarize_value(attributes))
        if status is not None:
            self.status = status

    def mark_partial(self, message: str) -> None:
        self.status = "partial"
        self.error = str(message)

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is not None:
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
        duration_ms = round((time.perf_counter_ns() - self._started_ns) / 1_000_000, 3)
        if self._token is not None:
            _ACTIVE_SPAN.reset(self._token)
        self.observer._append(
            NodeObservationEvent(
                trace_id=self.trace_id,
                session_id=self.session_id,
                case_id=self.case_id,
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
                node_name=self.node_name,
                node_type=self.node_type,
                phase="finished",
                status=self.status,
                duration_ms=duration_ms,
                output=summarize_value(self.output),
                metrics=self.metrics,
                attributes=summarize_value(self.attributes),
                error=self.error,
            )
        )
        if self.parent_span_id is None:
            self.observer._trace_completed(self.trace_id)
        return False


class ExecutionObserver:
    """Create nested workflow spans and persist them independently of vendors."""

    def __init__(
        self,
        path: str | Path,
        *,
        exporters: list[Callable[[str, ObservationLedger], None]] | None = None,
        export_queue_size: int = 256,
    ) -> None:
        if export_queue_size <= 0:
            raise ValueError("export_queue_size must be positive")
        self.ledger = ObservationLedger(path)
        self.exporters = list(exporters or [])
        self.events_written = 0
        self.events_dropped = 0
        self.export_failures = 0
        self.exports_completed = 0
        self.exports_dropped = 0
        self.last_error: str | None = None
        self._state = threading.Condition()
        self._pending_exports = 0
        self._closed = False
        self._exporters_closed = False
        self._export_queue: queue.Queue[str] | None = None
        self._export_thread: threading.Thread | None = None
        if self.exporters:
            self._export_queue = queue.Queue(maxsize=export_queue_size)
            self._export_thread = threading.Thread(
                target=self._export_loop,
                name="observation-exporter",
                daemon=True,
            )
            self._export_thread.start()

    @property
    def path(self) -> Path:
        return self.ledger.path

    def span(
        self,
        *,
        trace_id: str,
        case_id: str,
        node_name: str,
        node_type: str,
        session_id: str | None = None,
        parent_span_id: str | None = None,
        input: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ObservationSpan:
        active = _ACTIVE_SPAN.get()
        if parent_span_id is None and active is not None and active[0] == trace_id:
            parent_span_id = active[1]
        return ObservationSpan(
            self,
            trace_id=trace_id,
            session_id=session_id,
            case_id=case_id,
            node_name=node_name,
            node_type=node_type,
            parent_span_id=parent_span_id,
            input=input,
            attributes=attributes,
        )

    def health(self) -> dict[str, Any]:
        with self._state:
            return {
                "path": str(self.path),
                "events_written": self.events_written,
                "events_dropped": self.events_dropped,
                "exporters": len(self.exporters),
                "export_queue_capacity": (
                    self._export_queue.maxsize if self._export_queue is not None else 0
                ),
                "exports_pending": self._pending_exports,
                "exports_completed": self.exports_completed,
                "exports_dropped": self.exports_dropped,
                "export_failures": self.export_failures,
                "export_thread_alive": bool(
                    self._export_thread is not None and self._export_thread.is_alive()
                ),
                "closed": self._closed,
                "exporters_closed": self._exporters_closed,
                "last_error": self.last_error,
            }

    def _append(self, event: NodeObservationEvent) -> None:
        try:
            self.ledger.append(event)
            with self._state:
                self.events_written += 1
        except Exception as exc:  # observability must not break the business path
            with self._state:
                self.events_dropped += 1
                self.last_error = f"{type(exc).__name__}: {exc}"

    def _trace_completed(self, trace_id: str) -> None:
        export_queue = self._export_queue
        if export_queue is None:
            return
        with self._state:
            if self._closed:
                self.exports_dropped += 1
                self.last_error = "RuntimeError: observer is closed"
                return
            self._pending_exports += 1
            try:
                export_queue.put_nowait(trace_id)
            except queue.Full:
                self._pending_exports -= 1
                self.exports_dropped += 1
                self.last_error = "OverflowError: observation export queue is full"

    def _export_loop(self) -> None:
        export_queue = self._export_queue
        assert export_queue is not None
        while True:
            try:
                trace_id = export_queue.get(timeout=0.1)
            except queue.Empty:
                with self._state:
                    if self._closed and self._pending_exports == 0:
                        return
                continue
            try:
                for exporter in self.exporters:
                    try:
                        exporter(trace_id, self.ledger)
                    except Exception as exc:  # remote export is always best effort
                        with self._state:
                            self.export_failures += 1
                            self.last_error = f"{type(exc).__name__}: {exc}"
                with self._state:
                    self.exports_completed += 1
            finally:
                export_queue.task_done()
                with self._state:
                    self._pending_exports -= 1
                    self._state.notify_all()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait for queued exports, then flush vendor client buffers."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state:
            while self._pending_exports:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._state.wait(remaining)
            if self._exporters_closed:
                return True
        for exporter in self.exporters:
            flush = getattr(exporter, "flush", None)
            if not callable(flush):
                continue
            try:
                flush()
            except Exception as exc:
                with self._state:
                    self.export_failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                return False
        return True

    def close(self, timeout: float = 5.0) -> bool:
        """Stop accepting export jobs, drain the queue and close exporters."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = time.monotonic() + timeout
        with self._state:
            self._closed = True
            self._state.notify_all()
        flushed = self.flush(timeout=max(0.0, deadline - time.monotonic()))
        thread = self._export_thread
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            stopped = self._close_exporters()
        return flushed and stopped

    def _close_exporters(self) -> bool:
        with self._state:
            if self._exporters_closed:
                return True
            self._exporters_closed = True
        succeeded = True
        for exporter in self.exporters:
            close = getattr(exporter, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                with self._state:
                    self.export_failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                succeeded = False
        return succeeded

    def __enter__(self) -> "ExecutionObserver":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
