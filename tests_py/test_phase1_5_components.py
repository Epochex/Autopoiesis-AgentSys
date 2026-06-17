from __future__ import annotations

import json
import sys
from datetime import datetime
from subprocess import run

import pytest

from pathlib import Path

from core.context.compiler import ContextCompiler
from core.llm import LLMConfigurationError, StaticJsonLLMClient
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.controller import SkillAttentionController
from core.skills.spec import RegisteredSkill, SkillSpec
from core.verifier.verifier import Verifier
from domains.network_rca.adapters.fortios_syslog import LocalFixtureLogAdapter, parse_fortios_kv_line
from domains.network_rca.eval import compare_baselines
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases
from domains.network_rca.real_data_readiness import probe_r230_readiness
from domains.network_rca.real_dataset import load_real_case_bundle, validate_real_dataset_manifest
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


def test_real_dataset_manifest_validator_rejects_missing_and_template():
    missing = validate_real_dataset_manifest("/tmp/does-not-exist-selfevo-real-manifest.json")
    assert not missing.ready
    assert "does not exist" in missing.errors[0]

    template = validate_real_dataset_manifest("domains/network_rca/fixtures/real/manifest.example.json")
    assert not template.ready
    assert any("missing" in error for error in template.errors)


def test_fortios_syslog_parser_preserves_fields_and_filters_time_window(tmp_path):
    log_path = tmp_path / "fortigate.log"
    log_path.write_text(
        "\n".join(
            [
                'date=2026-06-14 time=10:00:00 type=traffic subtype=forward level=notice srcip=192.168.1.23 dstip=192.168.16.10 action=deny policyid=0 msg="Denied by forward policy check"',
                'date=2026-06-15 time=10:00:00 type=event subtype=system level=warning msg="other event"',
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_fortios_kv_line(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert parsed.timestamp == "2026-06-14T10:00:00"
    assert parsed.msg == "Denied by forward policy check"

    events = LocalFixtureLogAdapter(log_path).query(
        start=datetime.fromisoformat("2026-06-14T09:00:00"),
        end=datetime.fromisoformat("2026-06-14T11:00:00"),
        filters={"type": "traffic", "action": "deny"},
    )

    assert [event.policyid for event in events] == ["0"]


def test_real_manifest_ready_requires_real_train_and_heldout_splits(tmp_path):
    syslog = tmp_path / "fortigate.log"
    syslog.write_text('date=2026-06-14 time=10:00:00 type=traffic action=deny msg="heldout"\n', encoding="utf-8")
    seed = load_seed_cases()[0]
    train_case = {
        "case": seed.model_dump(),
        "ground_truth": {
            "expected_root_cause_key": "carrier_down",
            "required_evidence": ["ev-eno1-oper-down"],
            "split": "train",
            "dataset_kind": "real",
        },
    }
    heldout_case = {
        "case": seed.model_copy(update={"id": "real-heldout-carrier"}).model_dump(),
        "ground_truth": {
            "expected_root_cause_key": "carrier_down",
            "required_evidence": ["ev-eno1-oper-down"],
            "split": "heldout",
            "dataset_kind": "real",
        },
    }
    (tmp_path / "train_cases.json").write_text(json.dumps([train_case]), encoding="utf-8")
    (tmp_path / "heldout_cases.json").write_text(json.dumps([heldout_case]), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "unit-real",
                "dataset_kind": "real",
                "source_host": "192.168.1.23",
                "captured_days": 3,
                "syslog_paths": ["fortigate.log"],
                "train_cases_path": "train_cases.json",
                "heldout_cases_path": "heldout_cases.json",
            }
        ),
        encoding="utf-8",
    )

    validation = validate_real_dataset_manifest(manifest)
    cases, truth = load_real_case_bundle(manifest, split="heldout")

    assert validation.ready
    assert validation.warnings == ["captured_days is below the preferred 7-day upper target"]
    assert [case.id for case in cases] == ["real-heldout-carrier"]
    assert truth["real-heldout-carrier"].split == "heldout"


def test_real_heldout_eval_command_refuses_missing_manifest():
    result = run(
        [sys.executable, "-m", "domains.network_rca.eval_real_heldout", "/tmp/missing-selfevo-real-manifest.json"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "manifest file does not exist" in result.stdout


def test_ci_workflow_is_prepared_outside_github_until_workflow_scope_exists():
    workflow = Path("ci/github-workflows/python-phase15.yml")
    assert workflow.exists()
    text = workflow.read_text(encoding="utf-8")
    assert 'python-version: "3.11"' in text
    assert "pytest -p no:cacheprovider tests_py" in text


def test_python_version_config_targets_stable_311_series():
    version = Path(".python-version").read_text(encoding="utf-8").strip()
    assert version.startswith("3.11.")
    assert "rc" not in version.lower()

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11,<3.12"' in pyproject
