from __future__ import annotations

from core.eval.cross_domain import run_cross_domain_eval
from domains.active_recon.factory import build_active_recon_orchestrator, load_recon_seed_cases
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases


def test_both_domains_use_same_orchestrator_class_name(tmp_path):
    network_orch = build_network_rca_orchestrator(tmp_path / "network_trace.jsonl")
    recon_orch = build_active_recon_orchestrator(tmp_path / "recon_trace.jsonl")

    assert type(network_orch).__name__ == "SingleAgentRCAOrchestrator"
    assert type(network_orch).__name__ == type(recon_orch).__name__


def test_first_seed_case_verifies_in_both_domains(tmp_path):
    network_orch = build_network_rca_orchestrator(tmp_path / "network_first_trace.jsonl")
    recon_orch = build_active_recon_orchestrator(tmp_path / "recon_first_trace.jsonl")

    _, network_report = network_orch.diagnose(load_seed_cases()[0])
    _, recon_report = recon_orch.diagnose(load_recon_seed_cases()[0])

    assert network_report.passed is True
    assert recon_report.passed is True


def test_cross_domain_eval_returns_mock_pass_rates():
    result = run_cross_domain_eval()

    assert set(result["domains"]) == {"network_rca", "active_recon"}
    assert result["kernel_reuse"]["orchestrator_class"] == "SingleAgentRCAOrchestrator"
    assert result["kernel_reuse"]["domains"] == 2
    assert result["kernel_reuse"]["kernel_changes"] == 0
    assert result["domains"]["network_rca"]["verifier_pass_rate"] == 1.0
    assert result["domains"]["active_recon"]["verifier_pass_rate"] == 1.0
