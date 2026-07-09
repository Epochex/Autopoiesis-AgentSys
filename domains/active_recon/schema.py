from __future__ import annotations

from pydantic import BaseModel


class ReconCase(BaseModel):
    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]


class ReconGroundTruth(BaseModel):
    case_id: str
    exposed_services: list[str]
    top_risk: str
    expected_evidence_ids: list[str]
    split: str = "seed"
    dataset_kind: str = "mock"

    @property
    def expected_root_cause_key(self) -> str:
        return self.top_risk

    @property
    def required_evidence(self) -> list[str]:
        return self.expected_evidence_ids
