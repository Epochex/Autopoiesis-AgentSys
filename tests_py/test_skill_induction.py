from __future__ import annotations

import pytest

from core.skills import induction
from core.skills.induction import capture_unmatched, induce_skill, promote_skill
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


def test_induced_candidate_that_handles_captured_case_is_promoted(tmp_path, monkeypatch):
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    registry = SkillRegistry()
    registry.register(
        SkillSpec(name="status_check", description="Read status", tags=["status", "check"], risk="read_only"),
        lambda **kwargs: SkillResult(skill_name="status_check"),
    )
    case = capture_unmatched("enterprise ops pricing quote for new policy", [])

    candidate = induce_skill(case, [skill.spec for skill in registry.all()])
    promoted = promote_skill(candidate, registry, [case])

    assert promoted is True
    assert registry.get(candidate.name).spec.name == candidate.name
    assert {"enterprise", "pricing", "quote"}.issubset(set(candidate.tags))


def test_candidate_that_breaks_golden_routing_is_rejected(tmp_path, monkeypatch):
    # "does not break existing capability": a candidate that would steal a golden
    # case away from its expected skill must be rejected even if it covers the capture.
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    registry = SkillRegistry()
    registry.register(
        SkillSpec(name="status_check", description="Read status", tags=["status", "check"], risk="read_only", cost=1.0),
        lambda **kwargs: SkillResult(skill_name="status_check"),
    )
    captured = capture_unmatched("enterprise pricing quote for new policy", [])
    golden = {"golden": True, "request": "check status", "query_terms": ["status", "check"], "expected_skill": "status_check"}

    hijacker = SkillSpec(
        name="greedy_candidate",
        description="Covers the capture but also outbids the golden route",
        tags=["enterprise", "pricing", "quote", "policy", "new", "status", "check"],
        risk="read_only",
        cost=0.1,
    )

    assert promote_skill(hijacker, registry, [captured, golden]) is False
    with pytest.raises(KeyError):
        registry.get("greedy_candidate")


def test_candidate_that_leaves_a_captured_case_unhandled_is_rejected(tmp_path, monkeypatch):
    # promotion requires the candidate to make ALL captured failures routable
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    registry = SkillRegistry()
    covered = capture_unmatched("enterprise pricing quote", [])
    uncovered = capture_unmatched("network carrier interface flap", [])

    candidate = induce_skill(covered, [])

    assert promote_skill(candidate, registry, [covered, uncovered]) is False
    assert registry.all() == []


def test_candidate_whose_handler_cannot_replay_the_capture_is_rejected(tmp_path, monkeypatch):
    # replay gate is execution-level, not just routing-level: a candidate whose
    # handler raises on the original failing request must not enter the library.
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")

    def broken_handler(spec):
        def run(**kwargs):
            raise RuntimeError("induced handler cannot serve the request")

        return run

    monkeypatch.setattr(induction, "_induced_handler", broken_handler)
    registry = SkillRegistry()
    case = capture_unmatched("enterprise pricing quote for new policy", [])
    candidate = induce_skill(case, [])

    assert promote_skill(candidate, registry, [case]) is False
    assert registry.all() == []


def test_induced_candidate_spec_reflects_the_failure_trace(tmp_path, monkeypatch):
    # the captured trace must genuinely FEED induction: unmet sub-goal and
    # missing-evidence terms that no existing skill covers become tags/schema,
    # and the attempted-but-failed skills are named in the description.
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    existing = [
        SkillSpec(name="status_check", description="Read status", tags=["status", "check"], risk="read_only"),
        SkillSpec(name="pricing_apply_policy", description="Apply pricing", tags=["enterprise", "pricing", "quote", "policy"], risk="write"),
    ]
    trace = [
        {"kind": "intent_tier_attempted", "payload": {"tier": "rule_fast_path", "hit": False, "unmatched_subgoals": ["check warehouse inventory forecast"]}},
        {"kind": "step_verified", "payload": {"skill": "status_check", "passed": False, "violations": ["postcondition failed"]}},
        {"kind": "planner_proposed", "payload": {"skills": [], "missing_evidence": ["replenishment forecast"]}},
    ]
    case = capture_unmatched("handle overseas order backlog", trace)

    with_trace = induce_skill(case, existing)
    without_trace = induce_skill({"request": case["request"], "query_terms": case["query_terms"]}, existing)

    reach_terms = {"warehouse", "inventory", "forecast", "replenishment"}
    assert reach_terms.issubset(set(with_trace.tags))
    assert not reach_terms & set(without_trace.tags)
    # terms the library already covers must NOT be re-added from the trace
    assert "check" not in with_trace.tags
    assert "status_check" in with_trace.description
    assert with_trace.input_schema["evidence_gaps"] == sorted(reach_terms)


def test_review_gate_still_rejects_duplicate_of_a_trace_fed_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    registry = SkillRegistry()
    trace = [
        {"kind": "intent_tier_attempted", "payload": {"tier": "rule_fast_path", "hit": False, "unmatched_subgoals": ["forecast warehouse inventory"]}},
    ]
    case = capture_unmatched("handle overseas order backlog", trace)

    first = induce_skill(case, [skill.spec for skill in registry.all()])
    assert promote_skill(first, registry, [case]) is True

    # a second candidate induced from the same failure is a duplicate capability:
    # the review must reject it and leave the library unchanged.
    second = induce_skill(case, [skill.spec for skill in registry.all()])
    assert second.name != first.name
    assert promote_skill(second, registry, [case]) is False
    assert {skill.spec.name for skill in registry.all()} == {first.name}


def test_capture_unmatched_rejects_blank_request(tmp_path, monkeypatch):
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    with pytest.raises(ValueError):
        capture_unmatched("   ", [])


def test_garbage_or_duplicate_candidate_is_rejected_by_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(induction, "INDUCTION_STORE", tmp_path / "induction_cases.jsonl")
    registry = SkillRegistry()
    existing = SkillSpec(
        name="pricing_apply_policy",
        description="Apply pricing",
        tags=["enterprise", "pricing", "quote", "policy"],
        risk="write",
    )
    registry.register(existing, lambda **kwargs: SkillResult(skill_name=existing.name))
    case = capture_unmatched("enterprise pricing quote", [])

    duplicate = SkillSpec(
        name="pricing_apply_policy",
        description="Duplicate pricing",
        tags=["enterprise", "pricing", "quote", "policy"],
        risk="write",
    )
    garbage = SkillSpec(name="empty_candidate", description="", tags=[])

    before = {skill.spec.name for skill in registry.all()}
    assert promote_skill(duplicate, registry, [case]) is False
    assert promote_skill(garbage, registry, [case]) is False
    assert {skill.spec.name for skill in registry.all()} == before
    with pytest.raises(KeyError):
        registry.get("empty_candidate")
