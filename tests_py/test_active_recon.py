from __future__ import annotations

from core.trace.ledger import JSONLTraceLedger
from domains.active_recon.factory import build_active_recon_orchestrator, load_recon_seed_cases
from domains.active_recon.situational import build_situational_picture


def test_active_recon_orchestrator_diagnoses_mock_case(tmp_path):
    orchestrator = build_active_recon_orchestrator(tmp_path / "active_recon_trace.jsonl")
    case = load_recon_seed_cases()[0]

    diagnosis, report = orchestrator.diagnose(case)

    assert report.passed
    assert diagnosis.root_cause_key == "critical_cve_exposed"
    assert diagnosis.evidence
    assert {item.evidence_id for item in diagnosis.evidence}.issubset(
        {item["evidence_id"] for item in orchestrator._last_evidence}
    )


def test_situational_picture_returns_expected_top_risk_for_exposed_fixture(tmp_path):
    orchestrator = build_active_recon_orchestrator(tmp_path / "situational_trace.jsonl")
    case = next(case for case in load_recon_seed_cases() if case.id == "recon_public_web_critical")
    orchestrator.diagnose(case)

    picture = build_situational_picture(orchestrator._last_evidence)

    assert picture["top_risk"] == "critical_cve_exposed"
    assert picture["risk_score"] == 95
    assert {exposure["service"] for exposure in picture["exposures"]} == {"203.0.113.10:80/http"}


def test_approval_required_probe_exploit_is_not_executed_online(tmp_path):
    ledger_path = tmp_path / "approval_gate_trace.jsonl"
    orchestrator = build_active_recon_orchestrator(ledger_path, seed_memory=False)
    assert orchestrator.skills.get("probe_exploit").spec.risk == "approval_required"
    case = load_recon_seed_cases()[0].model_copy(
        update={"query_terms": ["exploit", "intrusive"], "relevant_skills": ["probe_exploit"]}
    )

    orchestrator.diagnose(case)

    events = JSONLTraceLedger(ledger_path).replay()
    exposed = next(event for event in events if event.kind == "skills_exposed")
    tool_calls = [event for event in events if event.kind == "tool_called"]
    assert "probe_exploit" not in exposed.payload["skills"]
    assert all(event.payload["skill"] != "probe_exploit" for event in tool_calls)
    assert "ev-recon-web-exploit-skipped" not in {item["evidence_id"] for item in orchestrator._last_evidence}


def test_recon_diagnosis_cites_only_observed_evidence(tmp_path):
    orchestrator = build_active_recon_orchestrator(tmp_path / "citation_trace.jsonl")
    for case in load_recon_seed_cases():
        diagnosis, report = orchestrator.diagnose(case)
        observed = {item["evidence_id"] for item in orchestrator._last_evidence}
        cited = {item.evidence_id for item in diagnosis.evidence}

        assert report.passed
        assert cited
        assert cited.issubset(observed)
