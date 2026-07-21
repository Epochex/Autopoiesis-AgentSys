from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from core.observability.ledger import ObservationLedger
from core.observability.schema import NodeObservationEvent, NodeSpan, TraceDiagnostics


class TraceAnalyzer:
    """Reconstruct long traces and point at slow, failed, or incomplete nodes."""

    def __init__(self, ledger: ObservationLedger):
        self.ledger = ledger

    def trace(self, trace_id: str) -> TraceDiagnostics:
        events = self.ledger.replay(trace_id=trace_id)
        if not events:
            raise KeyError(f"unknown observation trace: {trace_id}")
        return self._diagnostics(trace_id, events)

    def _diagnostics(
        self,
        trace_id: str,
        events: list[NodeObservationEvent],
    ) -> TraceDiagnostics:
        spans = self._spans(events)
        if not spans:
            raise ValueError(f"trace {trace_id!r} has no node start events")
        roots = [span for span in spans if span.parent_span_id is None]
        root = min(roots or spans, key=lambda span: span.started_at)
        incomplete = [span.node_name for span in spans if span.finished_at is None]
        failed = [span.node_name for span in spans if span.status == "error"]
        partial = [span.node_name for span in spans if span.status == "partial"]
        finished = [span for span in spans if span.duration_ms is not None]
        slowest = sorted(finished, key=lambda span: span.duration_ms or 0.0, reverse=True)[:8]
        bottleneck_candidates = [span for span in finished if span.parent_span_id is not None]
        bottleneck = max(
            bottleneck_candidates,
            key=lambda span: span.duration_ms or 0.0,
            default=None,
        )
        status = (
            "error"
            if incomplete or failed
            else "partial"
            if partial
            else root.status
        )
        metrics: dict[str, int | float | bool] = {}
        for span in spans:
            for name, value in span.metrics.items():
                metrics[f"{span.node_name}.{name}"] = value
        return TraceDiagnostics(
            trace_id=trace_id,
            session_id=root.session_id,
            case_id=root.case_id,
            status=status,
            started_at=root.started_at,
            finished_at=root.finished_at,
            duration_ms=root.duration_ms,
            node_count=len(spans),
            incomplete_nodes=incomplete,
            failed_nodes=failed,
            partial_nodes=partial,
            slowest_nodes=[self._node_brief(span) for span in slowest],
            bottleneck=self._node_brief(bottleneck) if bottleneck is not None else None,
            metrics=metrics,
            nodes=spans,
        )

    def recent(self, *, limit: int = 50, session_id: str | None = None) -> list[TraceDiagnostics]:
        if limit < 1:
            raise ValueError("limit must be positive")
        # One replay, one grouping pass. Calling trace() for every id would scan
        # the complete JSONL file once per trace and turn a dashboard refresh
        # into quadratic I/O.
        grouped: dict[str, list[NodeObservationEvent]] = defaultdict(list)
        latest: dict[str, datetime] = {}
        for event in self.ledger.replay(session_id=session_id):
            grouped[event.trace_id].append(event)
            latest[event.trace_id] = max(
                latest.get(event.trace_id, event.timestamp),
                event.timestamp,
            )
        trace_ids = sorted(latest, key=latest.__getitem__, reverse=True)[:limit]
        return [self._diagnostics(trace_id, grouped[trace_id]) for trace_id in trace_ids]

    def session(self, session_id: str) -> dict[str, Any]:
        traces = self.recent(limit=10_000, session_id=session_id)
        traces.sort(key=lambda trace: trace.started_at)
        by_node: dict[str, list[float]] = defaultdict(list)
        for trace in traces:
            for node in trace.nodes:
                if node.duration_ms is not None:
                    by_node[node.node_name].append(node.duration_ms)
        performance = {
            name: {
                "runs": len(values),
                "avg_ms": round(sum(values) / len(values), 3),
                "max_ms": round(max(values), 3),
                "latest_ms": round(values[-1], 3),
                "delta_from_first_ms": round(values[-1] - values[0], 3),
            }
            for name, values in sorted(by_node.items())
        }
        evolution_series = [
            {
                "trace_id": trace.trace_id,
                "started_at": trace.started_at,
                "status": trace.status,
                "duration_ms": trace.duration_ms,
                "node_count": trace.node_count,
                "failed_nodes": list(trace.failed_nodes),
                "partial_nodes": list(trace.partial_nodes),
                "incomplete_nodes": list(trace.incomplete_nodes),
                "node_durations_ms": {
                    node.node_name: node.duration_ms
                    for node in trace.nodes
                    if node.duration_ms is not None
                },
                "metrics": dict(trace.metrics),
            }
            for trace in traces
        ]
        return {
            "session_id": session_id,
            "trace_count": len(traces),
            "failed_traces": sum(trace.status == "error" for trace in traces),
            "partial_traces": sum(trace.status == "partial" for trace in traces),
            "performance_by_node": performance,
            "evolution_series": evolution_series,
            "degradation_alerts": self._degradation_alerts(traces, by_node),
            "traces": [trace.model_dump(mode="json", exclude={"nodes"}) for trace in traces],
        }

    @staticmethod
    def _degradation_alerts(
        traces: list[TraceDiagnostics],
        by_node: dict[str, list[float]],
    ) -> list[dict[str, Any]]:
        """Flag large, sustained-enough changes without fitting on labels.

        Latency needs at least three observations. The latest run must be both
        50% slower than the median of its history and at least 50 ms slower, so
        tiny timer noise is not presented as a system regression.
        """
        alerts: list[dict[str, Any]] = []
        for node_name, values in sorted(by_node.items()):
            if len(values) < 3:
                continue
            baseline = float(median(values[:-1]))
            latest = float(values[-1])
            delta = latest - baseline
            ratio = latest / baseline if baseline > 0.0 else float("inf")
            if delta >= 50.0 and ratio >= 1.5:
                alerts.append(
                    {
                        "kind": "node_latency_regression",
                        "node_name": node_name,
                        "baseline_median_ms": round(baseline, 3),
                        "latest_ms": round(latest, 3),
                        "delta_ms": round(delta, 3),
                        "ratio": round(ratio, 3),
                    }
                )

        historical_durations = [
            trace.duration_ms
            for trace in traces[:-1]
            if trace.duration_ms is not None
        ]
        if len(historical_durations) >= 2 and traces[-1].duration_ms is not None:
            baseline = float(median(historical_durations))
            latest = float(traces[-1].duration_ms)
            delta = latest - baseline
            ratio = latest / baseline if baseline > 0.0 else float("inf")
            if delta >= 100.0 and ratio >= 1.5:
                alerts.append(
                    {
                        "kind": "trace_latency_regression",
                        "baseline_median_ms": round(baseline, 3),
                        "latest_ms": round(latest, 3),
                        "delta_ms": round(delta, 3),
                        "ratio": round(ratio, 3),
                    }
                )
        if len(traces) >= 2:
            severity = {"ok": 0, "running": 1, "partial": 2, "error": 3}
            if severity[traces[-1].status] > severity[traces[-2].status]:
                alerts.append(
                    {
                        "kind": "new_status_regression",
                        "trace_id": traces[-1].trace_id,
                        "status": traces[-1].status,
                        "previous_status": traces[-2].status,
                    }
                )
        return alerts

    @staticmethod
    def _spans(events: list[NodeObservationEvent]) -> list[NodeSpan]:
        starts: dict[str, NodeObservationEvent] = {}
        finishes: dict[str, NodeObservationEvent] = {}
        for event in events:
            if event.phase == "started":
                starts.setdefault(event.span_id, event)
            else:
                finishes[event.span_id] = event
        spans: list[NodeSpan] = []
        for span_id, start in starts.items():
            finish = finishes.get(span_id)
            spans.append(
                NodeSpan(
                    trace_id=start.trace_id,
                    session_id=start.session_id,
                    case_id=start.case_id,
                    span_id=span_id,
                    parent_span_id=start.parent_span_id,
                    node_name=start.node_name,
                    node_type=start.node_type,
                    status=finish.status if finish is not None else "running",
                    started_at=start.timestamp,
                    finished_at=finish.timestamp if finish is not None else None,
                    duration_ms=finish.duration_ms if finish is not None else None,
                    input=start.input,
                    output=finish.output if finish is not None else {},
                    metrics=finish.metrics if finish is not None else {},
                    attributes={**start.attributes, **(finish.attributes if finish else {})},
                    error=finish.error if finish is not None else None,
                )
            )
        spans.sort(key=lambda span: span.started_at)
        return spans

    @staticmethod
    def _node_brief(span: NodeSpan) -> dict[str, Any]:
        return {
            "span_id": span.span_id,
            "node_name": span.node_name,
            "node_type": span.node_type,
            "status": span.status,
            "duration_ms": span.duration_ms,
            "error": span.error,
        }
