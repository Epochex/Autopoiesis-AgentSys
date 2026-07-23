from __future__ import annotations

from pydantic import BaseModel, Field


class VerificationReport(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    evidence_recall: float = 0.0


class Verifier:
    def __init__(
        self,
        enabled: bool = True,
        *,
        evidence_contracts: dict[str, set[str]] | None = None,
    ):
        self.enabled = enabled
        self.evidence_contracts = {
            key: set(value) for key, value in (evidence_contracts or {}).items()
        }

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
        current_observation_ids = {
            item["evidence_id"]
            for item in evidence
            if item.get("evidence_kind") != "knowledge_document"
            and item.get("current_observation", True) is not False
        }
        if cited_ids and not cited_ids.intersection(current_observation_ids):
            errors.append(
                "diagnosis cites no current operational observation; "
                "knowledge documents cannot establish current device state"
            )
        contradictions = [
            item["evidence_id"]
            for item in evidence
            if item.get("contradicts") == diagnosis.root_cause_key and item["evidence_id"] in cited_ids
        ]
        if contradictions:
            errors.append(f"diagnosis cites contradictory evidence: {sorted(contradictions)}")
        claim_contract = self.evidence_contracts.get(diagnosis.root_cause_key)
        if claim_contract is not None:
            missing_support = claim_contract.difference(cited_ids)
            if missing_support:
                errors.append(
                    "root cause evidence contract not satisfied: "
                    f"{sorted(missing_support)}"
                )
        if required_evidence:
            matched = len(set(required_evidence).intersection(cited_ids))
            recall = matched / len(required_evidence)
        else:
            recall = 1.0
        if recall < 1.0:
            errors.append("required evidence not fully cited")

        return VerificationReport(passed=not errors, errors=errors, evidence_recall=round(recall, 4))
