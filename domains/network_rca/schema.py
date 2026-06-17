from __future__ import annotations

from pydantic import BaseModel, Field


class RCASeedCase(BaseModel):
    id: str
    title: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]


class RCAGroundTruth(BaseModel):
    case_id: str
    expected_root_cause_key: str
    required_evidence: list[str]


class DiagnosisEvidence(BaseModel):
    evidence_id: str
    source: str
    summary: str


class RCADiagnosis(BaseModel):
    case_id: str
    root_cause_key: str
    root_cause: str
    confidence: float = 0.0
    evidence: list[DiagnosisEvidence] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    readonly: bool = True
