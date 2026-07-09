from __future__ import annotations

from core.orchestrator.adaptive import build_adaptive_orchestrator
from core.trace.ledger import JSONLTraceLedger
from domains.active_recon.factory import build_active_recon_orchestrator, load_recon_seed_cases
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases


def _events(path):
    return JSONLTraceLedger(path).replay()


def test_no_escalation_matches_single_agent_result(tmp_path):
    case = load_seed_cases()[0]
    single = build_network_rca_orchestrator(tmp_path / "single.jsonl", seed_memory=False)
    adaptive = build_adaptive_orchestrator(
        build_network_rca_orchestrator(tmp_path / "adaptive.jsonl", seed_memory=False)
    )

    single_diagnosis, single_report = single.diagnose(case)
    adaptive_diagnosis, adaptive_report = adaptive.diagnose(case)

    assert adaptive_diagnosis.model_dump() == single_diagnosis.model_dump()
    assert adaptive_report.model_dump() == single_report.model_dump()
    assert "topology_escalated" not in [event.kind for event in _events(tmp_path / "adaptive.jsonl")]


def test_network_rca_escalates_ambiguous_base_result_and_resolves(tmp_path):
    ledger_path = tmp_path / "network_adaptive.jsonl"
    case = load_seed_cases()[0]
    adaptive = build_adaptive_orchestrator(
        build_network_rca_orchestrator(ledger_path, seed_memory=False, top_k=1),
        confidence_threshold=0.6,
        max_rounds=2,
    )

    diagnosis, report = adaptive.diagnose(case)

    events = _events(ledger_path)
    assert any(event.kind == "topology_escalated" for event in events)
    assert report.passed
    assert diagnosis.root_cause_key == "carrier_down"
    assert diagnosis.confidence >= 0.6


def test_escalation_is_bounded_for_unresolved_case(tmp_path):
    ledger_path = tmp_path / "bounded_adaptive.jsonl"
    max_rounds = 1
    case = load_seed_cases()[0].model_copy(
        update={
            "id": "case_no_matching_fixture",
            "query": "Unknown asset has a carrier and policy issue with no fixture evidence.",
            "query_terms": ["carrier", "policy", "route"],
            "assets": ["missing-device"],
            "relevant_skills": ["check_interface_status"],
        }
    )
    adaptive = build_adaptive_orchestrator(
        build_network_rca_orchestrator(ledger_path, seed_memory=False, top_k=1),
        confidence_threshold=0.6,
        max_rounds=max_rounds,
    )

    diagnosis, report = adaptive.diagnose(case)

    resolved = [event for event in _events(ledger_path) if event.kind == "escalation_resolved"][-1]
    assert not report.passed
    assert diagnosis.confidence < 0.6
    assert resolved.payload["rounds_used"] <= max_rounds


def test_active_recon_escalation_preserves_readonly_gate(tmp_path):
    ledger_path = tmp_path / "active_recon_adaptive.jsonl"
    base_case = next(case for case in load_recon_seed_cases() if case.id == "recon_public_web_critical")
    case = base_case.model_copy(
        update={
            "query_terms": ["web", "cve", "exposed", "exploit"],
            "relevant_skills": ["scan_ports", "probe_exploit"],
            "high_blast": True,
        }
    )
    adaptive = build_adaptive_orchestrator(
        build_active_recon_orchestrator(ledger_path, seed_memory=False, top_k=1),
        confidence_threshold=0.6,
        max_rounds=2,
    )

    diagnosis, report = adaptive.diagnose(case)

    events = _events(ledger_path)
    tool_names = [
        event.payload["skill"]
        for event in events
        if event.kind == "tool_called" and not event.payload.get("blocked")
    ]
    assert any(event.kind == "topology_escalated" for event in events)
    assert report.passed
    assert diagnosis.root_cause_key == "critical_cve_exposed"
    assert diagnosis.confidence >= 0.6
    assert "probe_exploit" not in tool_names
    assert "probe_weak_credentials" not in tool_names
