from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.orchestrator.planner import execute_chain
from core.trace.ledger import JSONLTraceLedger
from domains.enterprise_ops.eval import run_routed_eval
from domains.enterprise_ops.factory import (
    build_enterprise_intent_router,
    build_enterprise_ops_orchestrator,
    load_enterprise_seed_cases,
)


def _build(tmp_path, **router_kwargs):
    orchestrator = build_enterprise_ops_orchestrator(tmp_path / "router_trace.jsonl")
    router = build_enterprise_intent_router(
        orchestrator,
        induction_store=tmp_path / "induction_captures.jsonl",
        **router_kwargs,
    )
    return orchestrator, router


def _events(tmp_path):
    return JSONLTraceLedger(tmp_path / "router_trace.jsonl").replay()


class _StubDeepAgent:
    def __init__(self):
        self.calls: list[str] = []

    def diagnose(self, case):
        self.calls.append(case.id)
        return "diagnosis", "report"


def test_high_freq_deterministic_pricing_request_resolves_at_rule_tier(tmp_path):
    orchestrator, router = _build(tmp_path)
    case = next(item for item in load_enterprise_seed_cases() if item.id == "ops_quote_then_approval")

    outcome = router.route(case)

    assert outcome.tier == "rule_fast_path"
    assert outcome.resolved is True
    assert outcome.induced is False
    assert outcome.chain == ["pricing_apply_policy", "approval_submit"]
    # the routed chain executes under contract verification end to end
    result = execute_chain(outcome.chain, case, orchestrator)
    assert all(verdict.passed for verdict in result["verdicts"])
    events = _events(tmp_path)
    routed = [event for event in events if event.kind == "intent_routed"]
    assert routed[-1].payload["tier"] == "rule_fast_path"
    attempts = [event.payload for event in events if event.kind == "intent_tier_attempted"]
    assert attempts[0]["tier"] == "rule_fast_path" and attempts[0]["hit"] is True


def test_vague_single_goal_request_resolves_via_library_recall(tmp_path):
    _, router = _build(tmp_path)
    # the raw query decomposes onto nothing, but the structured intent terms
    # let the attention controller recall a relevant read-only skill.
    case = SimpleNamespace(
        id="ops_vague_status",
        query="帮我看看这个单子",
        query_terms=["status", "查询"],
        assets=["order-1002"],
        relevant_skills=[],
    )

    outcome = router.route(case)

    assert outcome.tier == "library_recall"
    assert outcome.chain == ["status_check"]
    tiers = [event.payload["tier"] for event in _events(tmp_path) if event.kind == "intent_tier_attempted"]
    assert tiers == ["rule_fast_path", "library_recall"]


def test_ambiguous_compound_high_impact_request_escalates_to_deep_agent(tmp_path):
    deep_agent = _StubDeepAgent()
    _, router = _build(tmp_path, deep_agent=deep_agent)
    case = SimpleNamespace(
        id="ops_compound_ambiguous",
        query="按新策略报价，然后排查为什么审批一直卡住",
        query_terms=["报价", "审批"],
        assets=["order-1001"],
        relevant_skills=[],
        high_blast=True,
    )

    outcome = router.route(case)

    assert outcome.tier == "deep_agent"
    assert outcome.resolved is True
    assert outcome.handler is deep_agent
    assert outcome.induced is False
    # the returned handler is the real escalation topology entry point
    outcome.handler.diagnose(case)
    assert deep_agent.calls == ["ops_compound_ambiguous"]
    events = _events(tmp_path)
    attempts = [event.payload for event in events if event.kind == "intent_tier_attempted"]
    assert [attempt["tier"] for attempt in attempts] == ["rule_fast_path", "library_recall", "deep_agent"]
    assert attempts[0]["hit"] is False and attempts[1]["hit"] is False
    assert {"compound", "diagnostic", "high_impact"}.issubset(set(attempts[2]["reasons"]))
    assert not (tmp_path / "induction_captures.jsonl").exists()


def test_total_miss_triggers_capture_induction_and_reroutes_at_induction_tier(tmp_path):
    orchestrator, router = _build(tmp_path)
    case = SimpleNamespace(
        id="ops_new_capability",
        query="inventory restock plan for overseas warehouse",
        query_terms=["inventory", "restock", "warehouse"],
        assets=["warehouse-88"],
        relevant_skills=[],
    )

    outcome = router.route(case)

    assert outcome.tier == "skill_induction"
    assert outcome.resolved is True
    assert outcome.induced is True
    assert outcome.chain, "re-route through the expanded registry must yield a chain"
    induced_name = outcome.chain[0]
    assert orchestrator.skills.get(induced_name).spec.name == induced_name
    # capture_unmatched persisted the miss (the self-expand trigger is wired)
    assert (tmp_path / "induction_captures.jsonl").read_text(encoding="utf-8").count("\n") == 1

    events = _events(tmp_path)
    kinds = [event.kind for event in events]
    assert kinds.index("unmatched_captured") < kinds.index("skill_induced") < kinds.index("skill_promoted")
    # the cascade is visible in order: all three static tiers missed first ...
    attempts = [event.payload for event in events if event.kind == "intent_tier_attempted"]
    assert [attempt["tier"] for attempt in attempts][:3] == ["rule_fast_path", "library_recall", "deep_agent"]
    assert all(attempt["hit"] is False for attempt in attempts[:3])
    # ... then the re-route resolves on the induced skill at the rule tier
    assert any(attempt["tier"] == "rule_fast_path" and attempt["hit"] for attempt in attempts[3:])
    routed = [event for event in events if event.kind == "intent_routed"][-1]
    assert routed.payload["tier"] == "skill_induction" and routed.payload["induced"] is True


def test_escalation_shaped_request_without_deep_agent_is_unresolved_not_induced(tmp_path):
    _, router = _build(tmp_path)  # no deep agent wired
    case = SimpleNamespace(
        id="ops_high_impact_write",
        query="按新策略报价",
        query_terms=["报价"],
        assets=["order-1001"],
        relevant_skills=[],
        high_blast=True,
    )

    outcome = router.route(case)

    # the library IS relevant, so a miss must not mint a duplicate skill
    assert outcome.tier == "unresolved"
    assert outcome.resolved is False
    assert outcome.induced is False
    assert not (tmp_path / "induction_captures.jsonl").exists()


def test_router_rejects_empty_query(tmp_path):
    _, router = _build(tmp_path)
    case = SimpleNamespace(id="ops_blank", query="   ", query_terms=[], assets=[], relevant_skills=[])

    with pytest.raises(ValueError):
        router.route(case)


def test_enterprise_routed_eval_flows_every_case_through_the_router(tmp_path):
    rows = run_routed_eval(induction_store=tmp_path / "captures.jsonl")

    by_id = {row["case_id"]: row for row in rows}
    assert by_id["ops_quote_then_approval"]["tier"] == "rule_fast_path"
    assert by_id["ops_quote_then_approval"]["executed"] is True
    assert by_id["ops_quote_then_approval"]["chain"] == ["pricing_apply_policy", "approval_submit"]
    # contract verification still catches the bad-price case on the routed path
    assert any("price out of range" in violation for violation in by_id["ops_bad_price"]["violations"])
    # the unmatched capability case self-expands the library live
    induced = by_id["ops_unmatched_inventory"]
    assert induced["tier"] == "skill_induction"
    assert induced["induced"] is True and induced["resolved"] is True
    assert (tmp_path / "captures.jsonl").exists()
