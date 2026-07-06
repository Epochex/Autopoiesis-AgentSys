"""Phase A — the self-evolution loop is real and safe.

Guards the two properties a self-evolving agent must have and must never break:
  1. it gets more efficient on a recurring event stream (fewer probes / lower cost),
  2. it never trades accuracy or citation-verification to do so.
"""
from __future__ import annotations

from core.evolve import compare_cold_vs_warm, run_evolving_stream
from domains.network_rca.factory import load_ground_truth, load_seed_cases


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
    # ...and the persistent core actually grew from experience
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


def test_evolution_off_is_flat():
    """With learning disabled the stream never improves (control)."""
    cases, gt = _cases_gt()
    by_pass = run_evolving_stream(cases, gt, passes=3, evolve=False)["by_pass"]
    probes = {p["probes"] for p in by_pass}
    assert len(probes) == 1  # identical every pass
    assert all(p["memory_end"] == 0 for p in by_pass)  # nothing learned
