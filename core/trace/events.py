from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


TraceKind = Literal[
    "alert_received",
    "memory_read",
    "memory_shortcut",
    "memory_resolved",
    "context_compiled",
    "skills_exposed",
    "tool_called",
    "verifier_result",
    "diagnosis_completed",
    "cost_observed",
    "topology_escalated",
    "escalation_resolved",
    "planner_proposed",
    "executor_ran",
    "critic_reviewed",
    "skill_chain_planned",
    "step_verified",
]


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    case_id: str
    kind: TraceKind
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
