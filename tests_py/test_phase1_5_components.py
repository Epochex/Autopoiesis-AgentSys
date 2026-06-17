from __future__ import annotations

import pytest

from core.context.compiler import ContextCompiler
from core.llm import LLMConfigurationError, StaticJsonLLMClient
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.controller import SkillAttentionController
from core.skills.spec import RegisteredSkill, SkillSpec
from core.verifier.verifier import Verifier
from domains.network_rca.eval import compare_baselines
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases
from domains.network_rca.real_data_readiness import probe_r230_readiness
from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


def test_context_compiler_drops_noise_but_keeps_required_evidence_under_budget():
    evidence = [
        {
            "evidence_id": f"noise-{index}",
            "source": "mock:noise",
            "summary": "irrelevant noisy line " * 20,
        }
        for index in range(20)
    ]
    evidence.append(
        {
            "evidence_id": "critical",
            "source": "mock:truth",
            "summary": "FortiGate policy deny is the decisive evidence.",
        }
    )

    packet = ContextCompiler(token_budget=18).compile(
        case_id="case-budget",
        query="diagnose policy failure",
        memories_by_tier={},
        current_evidence=evidence,
        required_evidence=["critical"],
    )

    assert "critical" in packet.included_evidence_ids
    assert packet.missing_evidence == []
    assert not any(evidence_id.startswith("noise-") for evidence_id in packet.included_evidence_ids)


def test_skill_controller_demotes_high_misuse_skill_out_of_top_k():
    good = RegisteredSkill(
        spec=SkillSpec(name="good_policy_check", description="good", tags=["policy"], success_count=10, misuse_count=0),
        handler=lambda **kwargs: None,
    )
    bad = RegisteredSkill(
        spec=SkillSpec(name="bad_policy_check", description="bad", tags=["policy"], success_count=1, misuse_count=20),
        handler=lambda **kwargs: None,
    )

    selected = SkillAttentionController(top_k=1).select([bad, good], ["policy"], [])

    assert [skill.spec.name for skill in selected] == ["good_policy_check"]


def test_tiered_memory_query_only_returns_matching_tier():
    store = TieredMemoryStore()
    store.seed(
        [
            MemoryRecord(memory_id="m-episodic", tier="episodic", text="case note", tags=["episodic-only"]),
            MemoryRecord(memory_id="m-semantic", tier="semantic", text="topology note", tags=["semantic-only"]),
            MemoryRecord(memory_id="m-procedural", tier="procedural", text="runbook note", tags=["procedural-only"]),
            MemoryRecord(memory_id="m-profile", tier="asset_profile", text="asset note", tags=["profile-only"]),
        ]
    )

    result = store.retrieve(["semantic-only"], [])

    assert [record.memory_id for record in result["semantic"]] == ["m-semantic"]
    assert result["episodic"] == []
    assert result["procedural"] == []
    assert result["asset_profile"] == []


def test_verifier_rejects_missing_and_contradictory_evidence():
    diagnosis = RCADiagnosis(
        case_id="case-x",
        root_cause_key="carrier_down",
        root_cause="bad conclusion",
        evidence=[DiagnosisEvidence(evidence_id="ev-contradict", source="mock", summary="link is actually up")],
    )
    report = Verifier().verify(
        diagnosis,
        evidence=[
            {
                "evidence_id": "ev-contradict",
                "source": "mock",
                "summary": "link is actually up",
                "contradicts": "carrier_down",
            }
        ],
        required_evidence=["ev-required"],
    )

    assert not report.passed
    assert any("contradictory" in error for error in report.errors)
    assert any("required evidence" in error for error in report.errors)


def test_llm_reasoner_mode_uses_provider_response_and_missing_config_fails(tmp_path):
    case = load_seed_cases()[0]
    client = StaticJsonLLMClient(
        {
            "root_cause_key": "carrier_down",
            "root_cause": "LLM-selected carrier down.",
            "confidence": 0.81,
            "evidence": [{"evidence_id": "ev-eno1-oper-down"}, {"evidence_id": "ev-eno1-no-phy"}],
            "recommended_actions": ["readonly check"],
            "readonly": True,
        }
    )
    orchestrator = build_network_rca_orchestrator(tmp_path / "llm_trace.jsonl", reasoner_mode="llm", llm_client=client)

    diagnosis, report = orchestrator.diagnose(case)

    assert report.passed
    assert diagnosis.root_cause_key == "carrier_down"
    assert diagnosis.confidence == 0.81

    with pytest.raises(LLMConfigurationError):
        build_network_rca_orchestrator(tmp_path / "missing_llm.jsonl", reasoner_mode="llm")


def test_phase15_mock_baselines_are_labeled_mock_not_real():
    rows = compare_baselines(load_seed_cases(), load_ground_truth())

    assert {row.name for row in rows} == {"selfevo_light_path", "full_context", "full_tools", "no_memory"}
    assert {row.dataset_kind for row in rows} == {"mock"}
    assert {row.split for row in rows} == {"seed"}


def test_real_data_readiness_reports_blocked_without_ingestor_or_export():
    readiness = probe_r230_readiness()

    assert readiness.blocked
    assert "no readonly ingestor" in readiness.reason.lower() or "no local" in readiness.reason.lower()
