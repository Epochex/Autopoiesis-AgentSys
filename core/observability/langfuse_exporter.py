from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any

from core.observability.analysis import TraceAnalyzer
from core.observability.ledger import ObservationLedger
from core.observability.schema import NodeSpan


class LangfuseTraceExporter:
    """Best-effort exporter from the canonical local trace into Langfuse.

    Langfuse is deliberately downstream: exporter failure cannot remove or alter
    the locally replayable trajectory. The SDK import is lazy, so the deterministic
    core does not require Langfuse unless export is explicitly enabled.
    """

    def __init__(self, client: Any, *, propagate_attributes: Any | None = None):
        self.client = client
        self._propagate_attributes = propagate_attributes

    @classmethod
    def from_environment(cls) -> "LangfuseTraceExporter | None":
        enabled = os.getenv("AUTOPOIESIS_ENABLE_LANGFUSE", "0").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None
        try:
            from langfuse import get_client, propagate_attributes
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Langfuse export is enabled but the optional 'observability' extra is not installed"
            ) from exc
        return cls(get_client(), propagate_attributes=propagate_attributes)

    def __call__(self, trace_id: str, ledger: ObservationLedger) -> None:
        trace = TraceAnalyzer(ledger).trace(trace_id)
        by_parent: dict[str | None, list[NodeSpan]] = {}
        for node in trace.nodes:
            by_parent.setdefault(node.parent_span_id, []).append(node)
        roots = by_parent.get(None, [])
        if not roots:
            return
        context = (
            self._propagate_attributes(
                session_id=trace.session_id,
                trace_name="autopoiesis-rca",
                metadata={"run_id": trace.trace_id, "case_id": trace.case_id},
            )
            if self._propagate_attributes is not None and trace.session_id
            else nullcontext()
        )
        with context:
            for root in roots:
                self._export_node(root, by_parent, parent=None)

    def flush(self) -> None:
        """Flush SDK buffers when the owning service performs a bounded drain."""
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        """Release the optional SDK client after all local export jobs finish."""
        shutdown = getattr(self.client, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def flush(self) -> None:
        """Flush SDK buffers when the observer is explicitly drained."""
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        """Release the optional vendor client outside the request path."""
        shutdown = getattr(self.client, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def _export_node(
        self,
        node: NodeSpan,
        by_parent: dict[str | None, list[NodeSpan]],
        *,
        parent: Any | None,
    ) -> None:
        starter = parent.start_observation if parent is not None else self.client.start_observation
        observation = starter(
            name=node.node_name,
            as_type=self._langfuse_type(node.node_type),
            start_time=node.started_at,
            input=node.input,
            metadata={
                "run_id": node.trace_id,
                "case_id": node.case_id,
                "node_type": node.node_type,
                "status": node.status,
                **node.attributes,
                **node.metrics,
            },
        )
        for child in by_parent.get(node.span_id, []):
            self._export_node(child, by_parent, parent=observation)
        observation.update(output=node.output)
        end_kwargs = {"end_time": node.finished_at} if node.finished_at is not None else {}
        observation.end(**end_kwargs)

    @staticmethod
    def _langfuse_type(node_type: str) -> str:
        if node_type == "llm":
            return "generation"
        if node_type == "embedding":
            return "embedding"
        return "span"
