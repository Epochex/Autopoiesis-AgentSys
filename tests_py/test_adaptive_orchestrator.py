from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.context.compiler import ContextCompiler
from core.orchestrator.agents import CriticAgent, ExecutorAgent, PlannerAgent
from core.orchestrator.adaptive import build_adaptive_orchestrator
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec
from core.trace.ledger import JSONLTraceLedger
from core.verifier.verifier import Verifier
from domains.active_recon.factory import build_active_recon_orchestrator, load_recon_seed_cases
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases
from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


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
    kinds = [event.kind for event in events]
    planner_index = kinds.index("planner_proposed")
    executor_index = kinds.index("executor_ran")
    critic_index = kinds.index("critic_reviewed")
    assert any(event.kind == "topology_escalated" for event in events)
    assert planner_index < executor_index < critic_index
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


def test_planner_agent_proposes_missing_evidence_skill():
    records = []
    registry = SkillRegistry()
    registry.register(
        SkillSpec(
            name="check_interface_status",
            description="Check carrier interface status",
            tags=["carrier", "interface"],
            risk="read_only",
            cost=0.2,
        ),
        lambda case: SkillResult(skill_name="check_interface_status"),
    )
    registry.register(
        SkillSpec(
            name="mutate_interface",
            description="Change interface status",
            tags=["carrier", "interface"],
            risk="write",
        ),
        lambda case: SkillResult(skill_name="mutate_interface"),
    )
    case = SimpleNamespace(
        id="case-planner",
        query_terms=["carrier", "status"],
        relevant_skills=["check_interface_status"],
    )
    diagnosis = SimpleNamespace(missing_evidence=["carrier status"])

    proposed = PlannerAgent(
        registry,
        batch_size=2,
        record=lambda case_id, kind, payload: records.append((case_id, kind, payload)),
    ).propose(case, diagnosis, executed=set())

    assert proposed == ["check_interface_status"]
    assert records[-1][1] == "planner_proposed"
    assert records[-1][2]["skills"] == proposed


def test_executor_agent_blocks_non_readonly_skill():
    records = []
    registry = SkillRegistry()
    registry.register(
        SkillSpec(
            name="probe_exploit",
            description="Exploit probe",
            tags=["exploit"],
            risk="approval_required",
        ),
        lambda case: SkillResult(skill_name="probe_exploit", readonly=False),
    )
    case = SimpleNamespace(id="case-executor")

    with pytest.raises(PermissionError):
        ExecutorAgent(
            record=lambda case_id, kind, payload: records.append((case_id, kind, payload))
        ).run(case, ["probe_exploit"], registry)

    assert records[-1][1] == "executor_ran"
    assert records[-1][2]["blocked"] is True
    assert records[-1][2]["skill"] == "probe_exploit"


def test_critic_agent_verdict_requires_verifier_pass_and_confidence_threshold():
    evidence = [{"evidence_id": "e1", "source": "mock", "summary": "Interface carrier is down."}]
    case = SimpleNamespace(
        id="case-critic",
        query="Why is the router down?",
    )

    def builder_with_confidence(confidence: float, cite_evidence: bool = True):
        def build(case, evidence, context):
            cited = [
                DiagnosisEvidence(
                    evidence_id="e1",
                    source="mock",
                    summary="Interface carrier is down.",
                )
            ] if cite_evidence else []
            return RCADiagnosis(
                case_id=case.id,
                root_cause_key="carrier_down",
                root_cause="Carrier is down.",
                confidence=confidence,
                evidence=cited,
                readonly=True,
            )

        return build

    critic = CriticAgent(confidence_threshold=0.6)
    _, passing_report, passing_verdict = critic.review(
        case,
        evidence,
        ContextCompiler(),
        builder_with_confidence(0.7),
        Verifier(),
        {},
    )
    _, low_conf_report, low_conf_verdict = critic.review(
        case,
        evidence,
        ContextCompiler(),
        builder_with_confidence(0.5),
        Verifier(),
        {},
    )
    _, failing_report, failing_verdict = critic.review(
        case,
        evidence,
        ContextCompiler(),
        builder_with_confidence(0.9, cite_evidence=False),
        Verifier(),
        {},
    )

    assert passing_report.passed is True
    assert passing_verdict["passed"] is True
    assert low_conf_report.passed is True
    assert low_conf_verdict["passed"] is False
    assert failing_report.passed is False
    assert failing_verdict["passed"] is False
