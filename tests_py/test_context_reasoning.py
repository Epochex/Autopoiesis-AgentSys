from __future__ import annotations

import json

from core.context.compiler import ContextCompiler
from domains.network_rca.reasoner import LLMReasoner, build_diagnosis
from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis, RCASeedCase
from domains.network_rca.reasoner import ROOT_CAUSE_EVIDENCE_CONTRACTS
from core.verifier.verifier import Verifier


class _CapturingClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        self.requests.append(
            {
                "payload": json.loads(messages[0]["content"]),
                "schema_name": schema_name,
            }
        )
        return {
            "root_cause_key": "carrier_down",
            "confidence": 0.9,
            "evidence_ids": ["ev-eno1-oper-down", "ev-eno1-no-phy"],
            "recommended_actions": ["readonly check"],
            "readonly": True,
        }


def _case() -> RCASeedCase:
    return RCASeedCase(
        id="case-context",
        title="carrier",
        query="diagnose carrier",
        query_terms=["carrier"],
        assets=["eno1"],
        relevant_skills=[],
    )


def _carrier_evidence() -> list[dict]:
    return [
        {
            "evidence_id": "ev-eno1-oper-down",
            "source": "probe:link",
            "summary": "eno1 operational state is down",
            "data": {"carrier": False},
        },
        {
            "evidence_id": "ev-eno1-no-phy",
            "source": "probe:phy",
            "summary": "eno1 has no physical link",
            "data": {"link_detected": False},
        },
    ]


def test_rule_reasoner_uses_context_evidence_selection():
    case = _case()
    evidence = _carrier_evidence()
    compiler = ContextCompiler(token_budget=300)
    partial = compiler.compile(case.id, case.query, {}, evidence[:1], [])
    complete = compiler.compile(case.id, case.query, {}, evidence, [])

    partial_result = build_diagnosis(case, evidence, partial)
    complete_result = build_diagnosis(case, evidence, complete)

    assert partial_result.root_cause_key == "unknown"
    assert complete_result.root_cause_key == "carrier_down"


def test_llm_payload_consumes_compiled_context_without_raw_evidence_duplication():
    case = _case()
    evidence = _carrier_evidence()
    compiler = ContextCompiler(token_budget=300)
    first = compiler.compile(case.id, "first effective context", {}, evidence, ["ev-missing"])
    second = compiler.compile(case.id, "second effective context", {}, evidence, ["ev-missing"])
    client = _CapturingClient()
    reasoner = LLMReasoner(client)

    first_result = reasoner(case, evidence, first)
    reasoner(case, evidence, second)

    first_payload = client.requests[0]["payload"]
    second_payload = client.requests[1]["payload"]
    assert "evidence" not in first_payload
    assert first_payload["compiled_context"]["summary"] != second_payload["compiled_context"]["summary"]
    assert first_payload["compiled_context"]["included_evidence_ids"] == [
        "ev-eno1-no-phy",
        "ev-eno1-oper-down",
    ]
    assert first_payload["compiled_context"]["included_memory_ids"] == []
    assert first_payload["compiled_context"]["missing_evidence"] == ["ev-missing"]
    assert first_payload["compiled_context"]["sections"]
    assert first_result.missing_evidence == ["ev-missing"]


def test_existing_but_unrelated_citation_fails_root_cause_contract():
    case = _case()
    evidence = _carrier_evidence()
    context = ContextCompiler(token_budget=300).compile(
        case.id, case.query, {}, evidence, []
    )
    client = _CapturingClient()
    client.complete_json = lambda messages, schema_name: {
        "root_cause_key": "carrier_down",
        "confidence": 0.9,
        "evidence_ids": ["ev-eno1-oper-down"],
        "recommended_actions": [],
        "readonly": True,
    }
    diagnosis = LLMReasoner(client)(case, evidence, context)

    report = Verifier(
        evidence_contracts=ROOT_CAUSE_EVIDENCE_CONTRACTS
    ).verify(diagnosis, evidence, [])

    assert report.passed is False
    assert any("evidence contract" in error for error in report.errors)


def test_knowledge_document_cannot_replace_current_operational_observation():
    evidence = [{
        "evidence_id": "kb:carrier-runbook",
        "source": "runbook://carrier",
        "summary": "Carrier loss may indicate an unplugged cable.",
        "evidence_kind": "knowledge_document",
        "current_observation": False,
    }]
    diagnosis = RCADiagnosis(
        case_id="case-context",
        root_cause_key="unknown",
        root_cause="Unknown root cause",
        confidence=0.2,
        evidence=[DiagnosisEvidence(
            evidence_id="kb:carrier-runbook",
            source="runbook://carrier",
            summary="Carrier loss may indicate an unplugged cable.",
        )],
    )

    report = Verifier().verify(diagnosis, evidence, [])

    assert report.passed is False
    assert any("no current operational observation" in error for error in report.errors)
