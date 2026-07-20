from __future__ import annotations

import json

import pytest

from core.eval.replay import evaluate_trace, run_and_evaluate_replay
from core.skills.spec import SkillResult, SkillSpec
from core.trace.ledger import JSONLTraceLedger
from domains.network_rca.adapters.fortios_syslog import (
    LocalFixtureLogAdapter,
    R230IngestorLogAdapter,
    parse_fortios_kv_line,
)
from domains.network_rca.adapters.live_device import LiveDeviceAdapter
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases


def test_phase0_single_case_produces_complete_trace(tmp_path):
    ledger_path = tmp_path / "trace.jsonl"
    orchestrator = build_network_rca_orchestrator(ledger_path)
    case = load_seed_cases()[0]

    diagnosis, report = orchestrator.diagnose(case)

    assert report.passed
    assert diagnosis.root_cause_key == "carrier_down"
    truth = load_ground_truth()[case.id]
    assert {item.evidence_id for item in diagnosis.evidence} == set(truth.required_evidence)

    events = JSONLTraceLedger(ledger_path).replay()
    kinds = [event.kind for event in events]
    assert kinds == [
        "alert_received",
        "memory_read",
        "skills_exposed",
        "tool_called",
        "tool_called",
        "context_compiled",
        "verifier_result",
        "cost_observed",
        "diagnosis_completed",
    ]
    assert all(event.case_id == case.id for event in events)


def test_phase1_all_seed_cases_pass_with_evidence(tmp_path):
    ledger_path = tmp_path / "phase1_trace.jsonl"
    orchestrator = build_network_rca_orchestrator(ledger_path)
    cases = load_seed_cases()

    metrics = run_and_evaluate_replay(orchestrator, cases, load_ground_truth())

    assert metrics.cases == 5
    assert metrics.root_cause_accuracy == 1.0
    assert metrics.evidence_recall == 1.0
    assert metrics.verifier_pass_rate == 1.0

    events = JSONLTraceLedger(ledger_path).replay()
    assert len([event for event in events if event.kind == "diagnosis_completed"]) == 5
    assert len([event for event in events if event.kind == "tool_called"]) <= 15


def test_core_components_can_be_disabled_for_ablation(tmp_path):
    orchestrator = build_network_rca_orchestrator(
        tmp_path / "ablation_trace.jsonl",
        memory_enabled=False,
        context_enabled=False,
        skill_controller_enabled=False,
        verifier_enabled=False,
        top_k=99,
    )

    diagnosis, report = orchestrator.diagnose(load_seed_cases()[0])

    assert report.passed
    assert diagnosis.readonly
    events = JSONLTraceLedger(tmp_path / "ablation_trace.jsonl").replay()
    memory_event = next(event for event in events if event.kind == "memory_read")
    assert memory_event.payload == {"episodic": [], "semantic": [], "procedural": [], "asset_profile": []}
    exposed = next(event for event in events if event.kind == "skills_exposed").payload["skills"]
    assert len(exposed) >= 10


def test_live_adapter_is_feature_flagged_off_by_default(monkeypatch):
    monkeypatch.delenv("AUTOPOIESIS_ENABLE_LIVE_DEVICE_ADAPTER", raising=False)
    monkeypatch.delenv("SELFEVO_ENABLE_LIVE_DEVICE_ADAPTER", raising=False)
    with pytest.raises(RuntimeError):
        LiveDeviceAdapter()
    monkeypatch.delenv("AUTOPOIESIS_ENABLE_R230_INGESTOR", raising=False)
    monkeypatch.delenv("SELFEVO_ENABLE_R230_INGESTOR", raising=False)
    with pytest.raises(RuntimeError):
        R230IngestorLogAdapter()


def test_non_readonly_tool_result_is_blocked(tmp_path):
    orchestrator = build_network_rca_orchestrator(tmp_path / "readonly_guard.jsonl")
    orchestrator.skills.register(
        SkillSpec(
            name="unsafe_mock",
            description="bad skill for guard test",
            risk="read_only",
            cost=0.1,
            tags=["carrier"],
        ),
        lambda case: SkillResult(skill_name="unsafe_mock", evidence=[], readonly=False, cost=0.1),
    )
    orchestrator.skill_controller.top_k = 1
    case = load_seed_cases()[0].model_copy(update={"relevant_skills": ["unsafe_mock"]})

    with pytest.raises(PermissionError):
        orchestrator.diagnose(case)


def test_trace_ledger_is_replayable_jsonl(tmp_path):
    ledger_path = tmp_path / "trace.jsonl"
    orchestrator = build_network_rca_orchestrator(ledger_path)
    orchestrator.diagnose(load_seed_cases()[1])

    raw_lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert raw_lines
    decoded = [json.loads(line) for line in raw_lines]
    assert decoded[0]["kind"] == "alert_received"
    assert decoded[-1]["kind"] == "diagnosis_completed"

    metrics = evaluate_trace(ledger_path, load_ground_truth())
    assert metrics.cases == 5
    assert metrics.root_cause_accuracy == 0.2


def test_fortios_syslog_parser_and_fixture_adapter_are_readonly():
    event = parse_fortios_kv_line(
        'date=2026-06-15 time=09:05:11 type=traffic subtype=forward level=warning '
        'srcip=192.168.1.23 dstip=192.168.16.10 action=deny policyid=0 msg="Denied by forward policy check"'
    )
    assert event.timestamp == "2026-06-15T09:05:11"
    assert event.type == "traffic"
    assert event.subtype == "forward"
    assert event.action == "deny"
    assert event.policyid == "0"
    assert event.msg == "Denied by forward policy check"

    adapter = LocalFixtureLogAdapter("domains/network_rca/fixtures/fortios_syslog_samples.log")
    denied = adapter.query(filters={"action": "deny"})
    assert len(denied) == 1
    assert denied[0].srcip == "192.168.1.23"
