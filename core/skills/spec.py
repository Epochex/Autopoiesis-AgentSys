from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


RiskLevel = Literal["read_only", "approval_required", "write"]


class SkillSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = "read_only"
    cost: float = 1.0
    tags: list[str] = Field(default_factory=list)
    success_count: int = 0
    misuse_count: int = 0
    frozen: bool = False


class SkillResult(BaseModel):
    skill_name: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    readonly: bool = True
    cost: float = 1.0


class RegisteredSkill(BaseModel):
    spec: SkillSpec
    handler: Callable[..., SkillResult]

    model_config = {"arbitrary_types_allowed": True}
