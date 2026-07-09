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
