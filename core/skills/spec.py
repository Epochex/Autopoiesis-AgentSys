from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


RiskLevel = Literal["read_only", "approval_required", "write"]


class SkillSpec(BaseModel):
    """Declarative description of a skill: routing tags, risk class, and learning counters."""

    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = "read_only"
    cost: float = Field(default=1.0, ge=0.0)
    tags: list[str] = Field(default_factory=list)
    success_count: int = Field(default=0, ge=0)
    misuse_count: int = Field(default=0, ge=0)
    frozen: bool = False


class SkillResult(BaseModel):
    """Outcome of one skill execution; `evidence` items must carry an `evidence_id`."""

    skill_name: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    readonly: bool = True
    cost: float = Field(default=1.0, ge=0.0)

    def evidence_ids(self) -> list[str]:
        """Return the `evidence_id` of every evidence item.

        Raises ValueError if any item lacks one — evidence identity is load-bearing
        for citation verification and trace replay.
        """
        ids: list[str] = []
        for item in self.evidence:
            evidence_id = item.get("evidence_id")
            if not evidence_id:
                raise ValueError(f"skill {self.skill_name!r} returned evidence without an 'evidence_id'")
            ids.append(str(evidence_id))
        return ids


class RegisteredSkill(BaseModel):
    """A spec bound to its executable handler inside a registry."""

    spec: SkillSpec
    handler: Callable[..., SkillResult]

    model_config = {"arbitrary_types_allowed": True}
