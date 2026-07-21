from datetime import datetime, timezone

from core.context.compiler import ContextCompiler
from core.memory.evolution import analyze_evolution, reconstruct_evolution
from core.memory.store import MemoryRecord, MemoryRelation


def _event(mid: str, at: int, event_type: str, asset: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=mid,
        tier="episodic",
        text=f"{event_type} on {asset}",
        asset_ids=[asset],
        first_observed_at=datetime(2026, 1, at, tzinfo=timezone.utc),
        last_observed_at=datetime(2026, 1, at, tzinfo=timezone.utc),
        event_type=event_type,
    )


def test_reconstructs_cross_asset_evolution_and_finds_early_change():
    drift = _event("drift", 1, "baseline_deviation", "edge-a")
    change = _event("change", 2, "config_change", "edge-a")
    spread = _event("spread", 3, "propagation", "service-b")
    failure = _event("failure", 4, "visible_failure", "api-c")
    drift.baseline_delta = {"packet_loss_pct": 1.8}
    drift.relations = [MemoryRelation(target_id="change", relation_type="precedes")]
    change.relations = [MemoryRelation(target_id="spread", relation_type="causes", evidence_ids=["ev-change-spread"])]
    spread.relations = [MemoryRelation(target_id="failure", relation_type="propagates_to", evidence_ids=["ev-spread-failure"])]

    chain = reconstruct_evolution([failure, change, drift, spread], current_assets=["api-c"])

    assert chain is not None
    assert chain.memory_ids == ("drift", "change", "spread", "failure")
    assert chain.early_change_id == "drift"

    finding = analyze_evolution(
        [failure, change, drift, spread], current_assets=["api-c"]
    )
    assert finding is not None
    assert finding.hidden_failure_pattern is True
    assert finding.verified is True
    assert finding.asset_path == ("edge-a", "service-b", "api-c")


def test_context_contains_attributable_timeline_but_ignores_similarity_as_causality():
    early = _event("early", 1, "config_change", "edge-a")
    current = _event("current", 2, "visible_failure", "api-c")
    early.relations = [MemoryRelation(target_id="current", relation_type="precedes")]
    current.relations = [MemoryRelation(target_id="early", relation_type="similar_to")]

    packet = ContextCompiler(token_budget=800).compile(
        "case-evolution",
        "why is api-c failing",
        {"episodic": [current, early]},
        [],
        [],
    )

    assert "evolution_chain early_change=early" in packet.summary
    assert "early|config_change|edge-a" in packet.summary
    assert "current|visible_failure|api-c" in packet.summary
