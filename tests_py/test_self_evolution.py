"""Phase A — the self-evolution loop is real and safe.

Guards the two properties a self-evolving agent must have and must never break:
  1. it gets more efficient on a recurring event stream (fewer probes / lower cost),
  2. it never trades accuracy or citation-verification to do so.
"""
from __future__ import annotations

from core.evolve import compare_cold_vs_warm, run_evolving_stream
from core.memory.store import MemoryRecord
from domains.network_rca.factory import load_ground_truth, load_seed_cases
from domains.network_rca.factory import build_network_rca_orchestrator


def _cases_gt():
    return load_seed_cases(), load_ground_truth()


def test_recurring_incidents_get_cheaper_at_fixed_accuracy():
    cases, gt = _cases_gt()
    res = compare_cold_vs_warm(cases, gt, passes=4)
    d = res["delta"]
    # efficiency improves...
    assert d["probes_warm"] < d["probes_cold"], d
    assert d["cost_warm"] <= d["cost_cold"], d
    # ...without trading accuracy (the guardrail)
    assert d["accuracy_warm"] == d["accuracy_cold"] == 1.0, d
    # ...and the warm memory actually grew from experience
    assert d["memory_grown"] > 0, d


def test_warm_first_pass_equals_cold_no_free_lunch():
    """The very first encounter must be identical to cold — learning only helps later."""
    cases, gt = _cases_gt()
    warm = run_evolving_stream(cases, gt, passes=3, evolve=True)["by_pass"]
    cold = run_evolving_stream(cases, gt, passes=3, evolve=False)["by_pass"]
    assert warm[0]["probes"] == cold[0]["probes"]
    # later passes are cheaper than the first
    assert warm[-1]["probes"] <= warm[0]["probes"]
    assert all(p["accuracy"] == 1.0 for p in warm)


def test_episodic_memory_is_a_hypothesis_and_never_current_evidence(tmp_path):
    """A matching historical snapshot must not let a new run skip fresh probes."""
    case = load_seed_cases()[0]
    truth = load_ground_truth()[case.id]
    orch = build_network_rca_orchestrator(tmp_path / "freshness.jsonl", seed_memory=False)
    stale_id = "ev-stale-from-prior-run"
    orch.memory.add(
        MemoryRecord(
            memory_id="epi-prior",
            tier="episodic",
            text=f"prior {case.query} -> {truth.expected_root_cause_key}",
            tags=[*case.query_terms, truth.expected_root_cause_key, f"root:{truth.expected_root_cause_key}"],
            asset_ids=list(case.assets),
            confidence=1.5,
            evidence_snapshot=[{"evidence_id": stale_id, "source": "historical", "summary": "old observation"}],
        )
    )

    diagnosis, report = orch.diagnose(case)
    tool_calls = [event for event in orch._run_events if event.kind == "tool_called" and not event.payload.get("blocked")]
    ranked = next(event for event in orch._run_events if event.kind == "memory_candidates_ranked")
    attributed = next(event for event in orch._run_events if event.kind == "memory_attributed")
    confirmed = next(event for event in orch._run_events if event.kind == "memory_resolved")

    assert tool_calls
    assert ranked.payload["candidates"]
    assert all("lexical_score" in item for item in ranked.payload["candidates"])
    assert report.passed and diagnosis.root_cause_key == truth.expected_root_cause_key
    assert attributed.payload["memory_ids"] == ["epi-prior"]
    assert attributed.payload["items"] == [
        {"memory_id": "epi-prior", "role": "episodic_hypothesis"}
    ]
    assert stale_id not in {item["evidence_id"] for item in orch._last_evidence}
    assert confirmed.payload["historical_evidence_ids"] == [stale_id]
    assert confirmed.payload["fresh_probe_count"] == len(tool_calls)
    assert confirmed.payload["freshness_verified"] is True
    assert stale_id not in confirmed.payload["current_evidence_ids"]


def test_evolution_off_is_flat():
    """With learning disabled the stream never improves (control)."""
    cases, gt = _cases_gt()
    by_pass = run_evolving_stream(cases, gt, passes=3, evolve=False)["by_pass"]
    probes = {p["probes"] for p in by_pass}
    assert len(probes) == 1  # identical every pass
    assert all(p["memory_end"] == 0 for p in by_pass)  # nothing learned
