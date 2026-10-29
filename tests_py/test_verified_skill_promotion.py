from __future__ import annotations

import json

import pytest

from core.skills import induction
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


def _candidate(**changes):
    return SkillSpec(name="heater_probe", description="Read heater temperature", tags=["heater", "temperature"], **changes)


def _case():
    return {"request": "read heater temperature", "query_terms": ["heater", "temperature"], "expected_temperature": 195}


def _implementation(path, candidate):
    def read_sensor(**kwargs):
        reading = json.loads(path.read_text())
        return SkillResult(skill_name=candidate.name, evidence=[{
            "evidence_id": "sensor-reading", "source": str(path), "temperature": reading["temperature"],
        }])

    def verify(case, result):
        return result.evidence[0]["temperature"] == case["expected_temperature"]

    return induction.SkillImplementation(handler=read_sensor, verify_result=verify)


def test_metadata_only_candidate_cannot_be_promoted():
    registry = SkillRegistry()
    assert induction.promote_skill(_candidate(), registry, [_case()]) is False
    assert registry.all() == []


def test_verified_handler_is_registered_and_reads_fresh_source(tmp_path):
    path = tmp_path / "sensor.json"
    path.write_text('{"temperature":195}')
    candidate, registry = _candidate(), SkillRegistry()
    assert induction.promote_skill(candidate, registry, [_case()], implementation=_implementation(path, candidate))
    path.write_text('{"temperature":201}')
    result = registry.execute(candidate.name, request="read heater temperature")
    assert result.evidence[0]["temperature"] == 201
    assert result.evidence[0]["source"] == str(path)


def test_independent_expected_result_rejects_wrong_reading(tmp_path):
    path = tmp_path / "sensor.json"
    path.write_text('{"temperature":500}')
    candidate, registry = _candidate(), SkillRegistry()
    assert not induction.promote_skill(candidate, registry, [_case()], implementation=_implementation(path, candidate))
    assert registry.all() == []


@pytest.mark.parametrize("risk", ["write", "approval_required"])
def test_side_effect_candidates_are_not_executed_during_review(risk):
    calls = []
    candidate, registry = _candidate(risk=risk), SkillRegistry()

    def write(**kwargs):
        calls.append(kwargs)
        return SkillResult(skill_name=candidate.name)

    implementation = induction.SkillImplementation(handler=write, verify_result=lambda case, result: True)
    assert not induction.promote_skill(candidate, registry, [_case()], implementation=implementation)
    assert calls == []
    assert registry.all() == []


@pytest.mark.parametrize("changes", [
    {"skill_name": "different"}, {"readonly": False}, {"evidence": []},
    {"evidence": [{"source": "sensor"}]},
    {"evidence": [{"evidence_id": "echo", "kind": "induced_skill_match"}]},
])
def test_invalid_or_echo_results_do_not_become_capabilities(changes):
    candidate, registry = _candidate(), SkillRegistry()
    values = {"skill_name": candidate.name, "evidence": [{"evidence_id": "sensor", "temperature": 195}]}
    values.update(changes)
    implementation = induction.SkillImplementation(
        handler=lambda **kwargs: SkillResult(**values), verify_result=lambda case, result: True,
    )
    assert not induction.promote_skill(candidate, registry, [_case()], implementation=implementation)
    assert registry.all() == []


@pytest.mark.parametrize("verdict", [False, None, "passed", 1])
def test_verifier_requires_explicit_boolean_success(verdict):
    candidate, registry = _candidate(), SkillRegistry()
    implementation = induction.SkillImplementation(
        handler=lambda **kwargs: SkillResult(skill_name=candidate.name, evidence=[{"evidence_id": "sensor"}]),
        verify_result=lambda case, result: verdict,
    )
    assert not induction.promote_skill(candidate, registry, [_case()], implementation=implementation)
    assert registry.all() == []


def test_golden_route_is_checked_before_candidate_execution():
    calls, registry = [], SkillRegistry()
    registry.register(SkillSpec(name="status", description="Read status", tags=["status"]), lambda **kw: SkillResult(skill_name="status"))
    candidate = SkillSpec(name="hijacker", description="Read heater", tags=["heater", "temperature", "status"], cost=0)
    implementation = induction.SkillImplementation(handler=lambda **kw: calls.append(kw), verify_result=lambda c, r: True)
    golden = {"golden": True, "request": "status", "query_terms": ["status"], "expected_skill": "status"}
    assert not induction.promote_skill(candidate, registry, [_case(), golden], implementation=implementation)
    assert calls == []


def test_description_cannot_hijack_a_real_golden_plan():
    from core.orchestrator.planner import plan_skill_chain

    registry = SkillRegistry()
    registry.register(SkillSpec(name="status", description="Read status", tags=["status"]), lambda **kw: SkillResult(skill_name="status"))
    candidate = SkillSpec(name="heater_probe", description="Read heater temperature status", tags=["heater", "temperature"], cost=0)
    implementation = induction.SkillImplementation(
        handler=lambda **kw: SkillResult(skill_name=candidate.name, evidence=[{"evidence_id": "heater"}]),
        verify_result=lambda c, r: True,
    )
    golden = {"golden": True, "request": "read status", "query_terms": ["status"], "expected_skill": "status"}
    assert plan_skill_chain("read status", registry) == ["status"]
    assert not induction.promote_skill(candidate, registry, [_case(), golden], implementation=implementation)
    assert plan_skill_chain("read status", registry) == ["status"]


def test_promoted_request_handler_executes_through_the_real_chain(tmp_path):
    from types import SimpleNamespace
    from core.orchestrator.planner import execute_chain
    from domains.enterprise_ops.factory import build_enterprise_ops_orchestrator

    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "trace.jsonl")
    candidate = _candidate()

    def read_temperature(request):
        assert request == "read heater temperature"
        return SkillResult(skill_name=candidate.name, evidence=[{"evidence_id": "sensor", "temperature": 195}])

    implementation = induction.SkillImplementation(
        handler=read_temperature, verify_result=lambda c, r: r.evidence[0]["temperature"] == 195,
    )
    assert induction.promote_skill(candidate, orchestrator.skills, [_case()], implementation=implementation)
    case = SimpleNamespace(id="ops_quote_then_approval", query="read heater temperature", assets=[], relevant_skills=[])
    result = execute_chain([candidate.name], case, orchestrator)
    assert result["completed"], result["verdicts"]
    assert result["results"][0].evidence[0]["temperature"] == 195
