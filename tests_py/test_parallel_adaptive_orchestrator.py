from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.orchestrator.adaptive import ResourceSnapshot, build_adaptive_orchestrator
from core.orchestrator.agents import (
    CriticAgent,
    ParallelExecutorAgent,
    RoleAssignment,
    RoleFinding,
)
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec
from core.trace.ledger import JSONLTraceLedger
from core.verifier.verifier import Verifier
from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


def _case(**updates):
    values = {
        "id": "parallel-case",
        "query": "Correlate timeline route configuration and CVE evidence.",
        "query_terms": ["timeline", "route", "configuration", "cve"],
        "assets": ["edge-1"],
        "relevant_skills": ["baseline_probe"],
        "complexity": "high",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _evidence(evidence_id: str, source: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "source": source,
        "summary": f"Current observation from {source}",
    }


def _builder(case, evidence, context):
    specialist_ids = {
        item["evidence_id"]
        for item in evidence
        if item["evidence_id"].startswith("ev-specialist-")
    }
    complete = len(specialist_ids) == 4
    cited = [item for item in evidence if item["evidence_id"] in specialist_ids]
    if not complete:
        cited = evidence[:1]
    return RCADiagnosis(
        case_id=case.id,
        root_cause_key="correlated_failure" if complete else "unknown",
        root_cause="Four-perspective correlation" if complete else "Insufficient evidence",
        confidence=0.95 if complete else 0.2,
        evidence=[
            DiagnosisEvidence(
                evidence_id=item["evidence_id"],
                source=item["source"],
                summary=item["summary"],
            )
            for item in cited
        ],
        missing_evidence=[] if complete else ["four specialist perspectives"],
        readonly=True,
    )


def _build_base(tmp_path, specialist_handler):
    registry = SkillRegistry()
    registry.register(
        SkillSpec(
            name="baseline_probe",
            description="Initial generic probe",
            tags=["baseline"],
            risk="read_only",
            cost=0.1,
        ),
        lambda case: SkillResult(
            skill_name="baseline_probe",
            evidence=[_evidence("ev-baseline", "baseline")],
            cost=0.1,
        ),
    )
    role_skills = (
        ("inspect_timeline", "timeline event sequence", "temporal"),
        ("inspect_route", "route topology link", "topology"),
        ("inspect_config", "configuration policy", "configuration"),
        ("inspect_cve", "security cve vulnerability", "security"),
    )
    for index, (name, description, role) in enumerate(role_skills):
        registry.register(
            SkillSpec(
                name=name,
                description=description,
                tags=description.split(),
                risk="read_only",
                cost=0.2,
            ),
            specialist_handler(name, role, index),
        )
    return SingleAgentRCAOrchestrator(
        memory=TieredMemoryStore(enabled=False),
        context_compiler=ContextCompiler(token_budget=2_048),
        skills=registry,
        skill_controller=SkillAttentionController(top_k=1),
        verifier=Verifier(),
        diagnosis_builder=_builder,
        ledger_path=tmp_path / "parallel.jsonl",
    )


def test_adaptive_path_executes_four_specialists_in_parallel(tmp_path):
    barrier = threading.Barrier(4)
    intervals: dict[str, tuple[float, float, int]] = {}
    lock = threading.Lock()

    def handler_factory(name, role, index):
        def run(case):
            started = time.perf_counter()
            worker = threading.get_ident()
            barrier.wait(timeout=2.0)
            time.sleep(0.04)
            finished = time.perf_counter()
            with lock:
                intervals[role] = (started, finished, worker)
            return SkillResult(
                skill_name=name,
                evidence=[_evidence(f"ev-specialist-{index}", role)],
                cost=0.2,
            )

        return run

    adaptive = build_adaptive_orchestrator(
        _build_base(tmp_path, handler_factory),
        max_rounds=1,
        planner_batch_size=4,
        max_parallel_agents=4,
        resource_probe=lambda: ResourceSnapshot(cpu=0.1, memory=0.2, source="test"),
    )

    diagnosis, report = adaptive.diagnose(_case())

    assert report.passed
    assert diagnosis.root_cause_key == "correlated_failure"
    assert set(intervals) == {"temporal", "topology", "configuration", "security"}
    assert len({item[2] for item in intervals.values()}) == 4
    assert max(item[0] for item in intervals.values()) < min(item[1] for item in intervals.values())

    events = JSONLTraceLedger(tmp_path / "parallel.jsonl").replay()
    escalation = next(event for event in events if event.kind == "topology_escalated")
    assert escalation.payload["parallel_limit"] == 4
    assert set(escalation.payload["specialist_roles"]) == {
        "temporal", "topology", "configuration", "security",
    }
    specialist_events = [
        event for event in events
        if event.kind == "executor_ran" and event.payload.get("mode") == "parallel_specialists"
    ]
    assert len(specialist_events) == 4
    assert all(event.payload["parallel_workers"] == 4 for event in specialist_events)


def test_critical_resource_watermark_reduces_specialists_to_one_and_refuses(tmp_path):
    def handler_factory(name, role, index):
        return lambda case: SkillResult(
            skill_name=name,
            evidence=[_evidence(f"ev-specialist-{index}", role)],
            cost=0.2,
        )

    adaptive = build_adaptive_orchestrator(
        _build_base(tmp_path, handler_factory),
        max_rounds=1,
        planner_batch_size=4,
        max_parallel_agents=4,
        resource_probe=lambda: ResourceSnapshot(cpu=0.96, memory=0.4, source="test"),
    )

    diagnosis, report = adaptive.diagnose(_case())

    assert not report.passed
    assert diagnosis.root_cause_key == "unknown"
    assert diagnosis.confidence == 0.0
    events = JSONLTraceLedger(tmp_path / "parallel.jsonl").replay()
    escalation = next(event for event in events if event.kind == "topology_escalated")
    assert escalation.payload["parallel_limit"] == 1
    resolved = next(event for event in events if event.kind == "escalation_resolved")
    assert resolved.payload["rejected"] is True
    terminal_diagnosis = [event for event in events if event.kind == "diagnosis_completed"][-1]
    assert terminal_diagnosis.payload["root_cause_key"] == "unknown"
    assert terminal_diagnosis.payload["confidence"] == 0.0


def test_parallel_executor_preflights_all_skills_before_starting_any_handler():
    registry = SkillRegistry()
    calls: list[str] = []
    registry.register(
        SkillSpec(name="safe", description="safe", risk="read_only"),
        lambda case: calls.append("safe") or SkillResult(skill_name="safe"),
    )
    registry.register(
        SkillSpec(name="write", description="write", risk="write"),
        lambda case: calls.append("write") or SkillResult(skill_name="write", readonly=False),
    )
    assignments = [
        RoleAssignment(role="temporal", skill_names=("safe",)),
        RoleAssignment(role="configuration", skill_names=("write",)),
    ]

    with pytest.raises(PermissionError, match="non-readonly skill blocked"):
        ParallelExecutorAgent().run(_case(), assignments, registry, max_workers=2)

    assert calls == []


def test_critic_surfaces_cross_role_claim_conflict_and_fails_closed():
    evidence = [
        {
            **_evidence("ev-config-a", "configuration"),
            "diagnostic_role": "configuration",
            "claim_key": "policy.action",
            "claim_value": "allow",
        },
        {
            **_evidence("ev-security-a", "security"),
            "diagnostic_role": "security",
            "claim_key": "policy.action",
            "claim_value": "deny",
        },
    ]
    findings = [
        RoleFinding(role="configuration", skill_names=["config"], evidence=[evidence[0]]),
        RoleFinding(role="security", skill_names=["security"], evidence=[evidence[1]]),
    ]

    def confident_builder(case, evidence, context):
        item = evidence[0]
        return RCADiagnosis(
            case_id=case.id,
            root_cause_key="policy_mismatch",
            root_cause="Policy mismatch",
            confidence=0.95,
            evidence=[
                DiagnosisEvidence(
                    evidence_id=item["evidence_id"],
                    source=item["source"],
                    summary=item["summary"],
                )
            ],
            readonly=True,
        )

    _, report, verdict = CriticAgent(confidence_threshold=0.6).review(
        _case(id="conflict-case"),
        evidence,
        ContextCompiler(),
        confident_builder,
        Verifier(),
        {},
        findings,
    )

    assert not report.passed
    assert not verdict["passed"]
    assert verdict["conflicts"] == ["claim:policy.action:configuration!=security"]
    assert any("cross-agent evidence conflict" in error for error in report.errors)


def test_factory_adaptive_wrapper_accepts_production_observability_arguments(tmp_path):
    from domains.network_rca.factory import build_network_rca_service, load_seed_cases

    service = build_network_rca_service(
        tmp_path / "service.jsonl",
        start_maintenance=False,
        seed_memory=False,
        adaptive_multiagent_enabled=True,
        adaptive_options={
            "max_rounds": 1,
            "max_parallel_agents": 2,
            "resource_probe": lambda: ResourceSnapshot(cpu=0.1, memory=0.1, source="test"),
        },
    )
    try:
        diagnosis, report = service.diagnose(load_seed_cases()[0], session_id="adaptive-session")
        assert diagnosis.case_id
        assert report.passed
        assert service.observer is service.orchestrator.observer
    finally:
        service.close()
