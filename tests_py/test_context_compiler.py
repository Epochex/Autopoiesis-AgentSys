from __future__ import annotations

from core.context.compiler import SECTION_NAMES, ContextCompiler
from core.memory.store import MemoryRecord
from domains.network_rca.eval import compare_context_strategies
from domains.network_rca.factory import load_ground_truth, load_seed_cases


def _memory(memory_id: str, tier: str, text: str) -> MemoryRecord:
    return MemoryRecord(memory_id=memory_id, tier=tier, text=text)


def _budgets(**overrides: int) -> dict[str, int]:
    budgets = {name: 1 for name in SECTION_NAMES}
    budgets.update(overrides)
    return budgets


def test_structured_compiler_uses_fixed_sections_and_trims_only_the_overflowing_section():
    compiler = ContextCompiler(
        token_budget=80,
        section_budgets=_budgets(
            **{
                "Task Spec": 5,
                "Assets & Entities": 8,
                "Workflow": 8,
                "Evidence": 8,
                "Learnings": 8,
                "Key Results": 8,
            }
        ),
    )
    memories = {
        "asset_profile": [_memory("asset-1", "asset_profile", "known asset")],
        "procedural": [_memory("proc-1", "procedural", "follow known workflow")],
        "semantic": [_memory("sem-1", "semantic", "stable learned fact")],
        "episodic": [_memory("epi-1", "episodic", "prior verified result")],
    }
    evidence = [
        {"evidence_id": "ev-long", "source": "probe", "summary": "noise " * 20},
        {"evidence_id": "ev-short", "source": "probe", "summary": "useful signal"},
    ]

    packet = compiler.compile("case", "diagnose now", memories, evidence, ["ev-long"])

    assert [section.name for section in packet.sections] == list(SECTION_NAMES)
    assert packet.compiler_mode == "structured"
    assert packet.included_memory_ids == ["asset-1", "proc-1", "sem-1", "epi-1"]
    assert packet.included_evidence_ids == ["ev-long"]
    evidence_section = next(section for section in packet.sections if section.name == "Evidence")
    assert [item.item_id for item in evidence_section.dropped] == ["ev-short"]
    assert evidence_section.dropped[0].reason == "section_budget"
    workflow = next(section for section in packet.sections if section.name == "Workflow")
    assert [item.item_id for item in workflow.kept] == ["proc-1"]


def test_required_evidence_is_never_evicted_in_structured_or_flat_mode():
    evidence = [
        {"evidence_id": f"required-{index:02d}", "source": "probe", "summary": "decisive " * 8}
        for index in range(12)
    ]
    required = [item["evidence_id"] for item in evidence]

    for strategy in ("flat", "structured"):
        packet = ContextCompiler(
            token_budget=40,
            max_evidence_lines=3,
            strategy=strategy,
        ).compile("case", "diagnose", {}, evidence, required)

        assert packet.included_evidence_ids == required
        assert packet.missing_evidence == []
        assert all(not item.required for section in packet.sections for item in section.dropped)


def test_disabled_ablation_preserves_unbounded_flat_summary():
    memories = {"asset_profile": [_memory("asset-1", "asset_profile", "known asset")]}
    evidence = [{"evidence_id": "ev-1", "source": "probe", "summary": "observed"}]

    packet = ContextCompiler(token_budget=1, enabled=False).compile(
        "case", "diagnose", memories, evidence, []
    )

    assert packet.compiler_mode == "disabled"
    assert packet.summary == "diagnose\nasset_profile: known asset\n[ev-1] probe: observed"
    assert packet.included_memory_ids == ["asset-1"]
    assert packet.included_evidence_ids == ["ev-1"]
    assert packet.sections[0].dropped == []


def test_structured_compilation_and_provenance_are_deterministic():
    memories = {
        "semantic": [
            _memory("sem-1", "semantic", "first learning"),
            _memory("sem-2", "semantic", "second learning that does not fit"),
        ]
    }
    evidence = [{"evidence_id": "ev-1", "source": "probe", "summary": "observed"}]
    compiler = ContextCompiler(token_budget=70)

    first = compiler.compile("case", "diagnose", memories, evidence, []).model_dump()
    second = compiler.compile("case", "diagnose", memories, evidence, []).model_dump()

    assert first == second
    learnings = next(section for section in first["sections"] if section["name"] == "Learnings")
    assert [item["item_id"] for item in learnings["kept"]] == ["sem-1", "sem-2"]
    assert learnings["dropped"] == []
    assert learnings["token_budget"] > compiler.section_budgets["Learnings"]


def test_mixed_language_counter_does_not_treat_chinese_paragraph_as_one_token():
    chinese_query = "持续观察核心交换机延迟抬升以及相邻链路间歇丢包"

    packet = ContextCompiler(token_budget=200).compile("case", chinese_query, {}, [], [])

    assert packet.estimated_tokens_before >= len(chinese_query)


def test_custom_token_counter_is_used_for_budgeting():
    compiler = ContextCompiler(token_budget=40, token_counter=lambda text: len(text))

    packet = compiler.compile("case", "中文", {}, [], [])

    assert packet.estimated_tokens_before == 2


def test_seed_smoke_comparison_uses_equal_inputs_and_preserves_quality():
    rows = compare_context_strategies(load_seed_cases(), load_ground_truth())
    by_strategy = {row.strategy: row for row in rows}
    flat = by_strategy["flat"]
    structured = by_strategy["structured"]

    assert flat.dataset_kind == structured.dataset_kind == "mock"
    assert flat.estimated_tokens_before == structured.estimated_tokens_before
    assert flat.context_packets == structured.context_packets == flat.cases
    assert structured.root_cause_accuracy >= flat.root_cause_accuracy
    assert structured.citation_verify_pass_rate >= flat.citation_verify_pass_rate
