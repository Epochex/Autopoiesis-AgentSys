from __future__ import annotations

from pydantic import BaseModel, Field


class VerificationReport(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    evidence_recall: float = 0.0


class Verifier:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def verify(self, diagnosis, evidence: list[dict], required_evidence: list[str]) -> VerificationReport:
        if not self.enabled:
            return VerificationReport(passed=True, evidence_recall=1.0)

        errors: list[str] = []
        evidence_ids = {item["evidence_id"] for item in evidence}
        cited_ids = {item.evidence_id for item in diagnosis.evidence}

        if not diagnosis.readonly:
            errors.append("diagnosis contains non-readonly action")
        if not diagnosis.root_cause_key:
            errors.append("missing root cause key")
        if not cited_ids:
            errors.append("diagnosis cites no evidence")
        missing_citations = cited_ids.difference(evidence_ids)
        if missing_citations:
            errors.append(f"cited evidence not observed: {sorted(missing_citations)}")
        contradictions = [
            item["evidence_id"]
            for item in evidence
            if item.get("contradicts") == diagnosis.root_cause_key and item["evidence_id"] in cited_ids
        ]
        if contradictions:
            errors.append(f"diagnosis cites contradictory evidence: {sorted(contradictions)}")
        if required_evidence:
            matched = len(set(required_evidence).intersection(cited_ids))
            recall = matched / len(required_evidence)
        else:
            recall = 1.0
        if recall < 1.0:
            errors.append("required evidence not fully cited")

        return VerificationReport(passed=not errors, errors=errors, evidence_recall=round(recall, 4))
