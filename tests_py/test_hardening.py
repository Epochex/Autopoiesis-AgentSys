from __future__ import annotations

from domains.active_recon.factory import build_active_recon_orchestrator, load_recon_seed_cases
from domains.active_recon.hardening import recommend_hardening
from domains.active_recon.situational import build_situational_picture


def test_hardening_recommends_approval_gated_critical_cve_action(tmp_path):
    orchestrator = build_active_recon_orchestrator(tmp_path / "hardening_trace.jsonl")
    case = next(case for case in load_recon_seed_cases() if case.id == "recon_public_web_critical")
    diagnosis, report = orchestrator.diagnose(case)
    assert report.passed
    assert diagnosis.root_cause_key == "critical_cve_exposed"

    evidence = orchestrator._last_evidence
    picture = build_situational_picture(evidence)
    recommendations = recommend_hardening(picture, evidence)

    top_recommendation = next(item for item in recommendations if item["priority"] == 1)
    observed_evidence_ids = {item["evidence_id"] for item in evidence}

    assert top_recommendation["risk"] == "critical_cve_exposed"
    assert "Patch" in top_recommendation["action"]
    assert top_recommendation["requires_approval"] is True
    assert set(top_recommendation["evidence_ids"]).issubset(observed_evidence_ids)


def test_hardening_never_marks_mutating_action_as_auto_executable(tmp_path):
    orchestrator = build_active_recon_orchestrator(tmp_path / "hardening_mutation_trace.jsonl")
    mutating_risks = {
        "critical_cve_exposed",
        "internet_exposed_admin",
        "public_database_exposure",
        "weak_tls_exposed",
    }

    for case in load_recon_seed_cases():
        orchestrator.diagnose(case)
        picture = build_situational_picture(orchestrator._last_evidence)
        recommendations = recommend_hardening(picture, orchestrator._last_evidence)

        assert not any(
            item["risk"] in mutating_risks and item["requires_approval"] is False
            for item in recommendations
        )
