from __future__ import annotations

from pydantic import BaseModel, Field


class EnterpriseOpsCase(BaseModel):
    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]
    approval_grants: dict[str, str] = Field(default_factory=dict)
