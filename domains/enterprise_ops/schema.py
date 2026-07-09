from __future__ import annotations

from pydantic import BaseModel


class EnterpriseOpsCase(BaseModel):
    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]
