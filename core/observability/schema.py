from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


ObservationPhase = Literal["started", "finished"]
ObservationStatus = Literal["running", "ok", "partial", "error"]


class NodeObservationEvent(BaseModel):
    """One immutable node lifecycle event.

    A start event is persisted before work begins and a finish event afterwards.
    Replaying an unmatched start therefore exposes a crash or a stuck node instead
    of silently losing the slowest part of a trajectory.
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str
    session_id: str | None = None
    case_id: str
    span_id: str
    parent_span_id: str | None = None
    node_name: str
    node_type: str
    phase: ObservationPhase
    status: ObservationStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, int | float | bool] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class NodeSpan(BaseModel):
    """Reconstructed start/finish pair for one workflow node."""

    trace_id: str
    session_id: str | None = None
    case_id: str
    span_id: str
    parent_span_id: str | None = None
    node_name: str
    node_type: str
    status: ObservationStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, int | float | bool] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class TraceDiagnostics(BaseModel):
    """A query-ready diagnosis of one complete or interrupted trajectory."""

    trace_id: str
    session_id: str | None = None
    case_id: str
    status: ObservationStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    node_count: int
    incomplete_nodes: list[str] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    partial_nodes: list[str] = Field(default_factory=list)
    slowest_nodes: list[dict[str, Any]] = Field(default_factory=list)
    bottleneck: dict[str, Any] | None = None
    metrics: dict[str, int | float | bool] = Field(default_factory=dict)
    nodes: list[NodeSpan] = Field(default_factory=list)
