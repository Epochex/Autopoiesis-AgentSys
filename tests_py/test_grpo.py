"""GRPO group-relative credit assignment — the math is real, safe, and lands on
the policy parameters that gate retrieval and stopping.

All tests are deterministic and LLM-free: the math tests synthesize trace
events; the end-to-end test replays a real orchestrator ledger (rule reasoner,
mock adapters) so the "replayable trajectories as the group sampling unit"
claim is exercised on the actual JSONL trace format.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

from core.evolve.consolidate import consolidate_run
from core.evolve.grpo import (
    CONFIDENCE_FLOOR,
    apply_advantages,
    build_groups,
    group_advantages,
    trajectory_reward,
)
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.trace.events import TraceEvent
from core.trace.ledger import JSONLTraceLedger
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases


def _ev(run_id: str, case_id: str, kind: str, payload: dict) -> TraceEvent:
    return TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)


def _run(run_id: str, case_id: str, *, root: str, passed: bool, cost: float, mem_ids: list[str]) -> list[TraceEvent]:
    return [
        _ev(run_id, case_id, "memory_read", {"episodic": mem_ids, "semantic": [], "procedural": [], "asset_profile": []}),
        _ev(run_id, case_id, "cost_observed", {"tool_cost": cost, "tool_calls": int(cost)}),
        _ev(run_id, case_id, "verifier_result", {"passed": passed}),
        _ev(run_id, case_id, "diagnosis_completed", {"root_cause_key": root}),
    ]


_TRUTH = {"c1": SimpleNamespace(expected_root_cause_key="carrier_down")}


# ── advantage math: group-relative, numerically safe ─────────────────────────
def test_advantages_are_group_relative_and_mean_zero():
    adv = group_advantages([1.5, 0.5, 1.0])
    assert abs(sum(adv)) < 1e-9                    # the group IS the baseline
    assert adv[0] > 0 > adv[1]                     # above/below the group mean
    assert adv[0] > adv[2] > adv[1]                # ordering preserved


def test_advantages_degenerate_groups_never_produce_nan_or_inf():
    assert group_advantages([]) == []
    assert group_advantages([0.7]) == [0.0]        # singleton: no intra-group signal
    uniform = group_advantages([0.5, 0.5, 0.5, 0.5])
    assert uniform == [0.0, 0.0, 0.0, 0.0]         # zero-std group: exact zeros
    assert all(math.isfinite(a) for a in group_advantages([1e12, -1e12]))


def test_advantage_normalization_is_scale_invariant():
    small = group_advantages([0.0, 1.0])
    large = group_advantages([0.0, 100.0])
    assert all(abs(a - b) < 1e-6 for a, b in zip(small, large))
    # while the un-normalized baseline-only variant keeps the raw scale
    assert group_advantages([0.0, 100.0], normalize=False) == [-50.0, 50.0]


# ── reward: real trace signals only ──────────────────────────────────────────
def test_reward_prefers_correct_verified_and_cheap():
    good_cheap = trajectory_reward(_run("r1", "c1", root="carrier_down", passed=True, cost=0.0, mem_ids=[]), "carrier_down")
    good_costly = trajectory_reward(_run("r2", "c1", root="carrier_down", passed=True, cost=10.0, mem_ids=[]), "carrier_down")
    wrong = trajectory_reward(_run("r3", "c1", root="wrong_key", passed=False, cost=10.0, mem_ids=[]), "carrier_down")
    assert good_cheap > good_costly > wrong
    assert good_cheap == 1.5                       # +1.0 correct, +0.5 verified
    assert good_costly == 1.3                      # minus 10 × 0.02 cost penalty


# ── group construction from a flat replayed ledger ───────────────────────────
def test_build_groups_partitions_a_flat_ledger_by_case_and_run():
    events = [
        *_run("r1", "c1", root="carrier_down", passed=True, cost=2.0, mem_ids=[]),
        *_run("r2", "c1", root="carrier_down", passed=True, cost=0.0, mem_ids=["epi-1"]),
        *_run("r3", "c2", root="anything", passed=False, cost=1.0, mem_ids=[]),  # no ground truth
    ]
    groups = build_groups(events, _TRUTH)
    assert len(groups) == 1                        # c2 has no truth → no reward → skipped
    group = groups[0]
    assert group.case_id == "c1" and len(group.samples) == 2
    first, second = group.samples
    assert (first.run_id, second.run_id) == ("r1", "r2")   # replay order preserved
    assert second.reward > first.reward            # cheaper rollout beats the group
    assert second.advantage > 0 > first.advantage  # group-relative, not absolute
    assert abs(first.advantage + second.advantage) < 1e-6
    assert second.memory_ids == ("epi-1",)         # read-set captured for credit


# ── the update lands on the retrieval/stop policy parameters ─────────────────
def test_apply_advantages_reinforces_winners_weakens_losers_and_clamps():
    mem = TieredMemoryStore()
    mem.add(MemoryRecord(memory_id="epi-win", tier="episodic", text="good prior", confidence=1.0))
    mem.add(MemoryRecord(memory_id="epi-lose", tier="episodic", text="bad prior", confidence=0.25))
    events = [
        *_run("r1", "c1", root="carrier_down", passed=True, cost=0.0, mem_ids=["epi-win"]),
        *_run("r2", "c1", root="wrong_key", passed=False, cost=6.0, mem_ids=["epi-lose"]),
    ]
    report = apply_advantages(mem, build_groups(events, _TRUTH))
    assert report["reinforced"] == ["epi-win"] and report["weakened"] == ["epi-lose"]
    assert mem.get("epi-win").confidence > 1.0     # easier to retrieve / trust next time
    assert mem.get("epi-win").strength == 1.0      # profitable reuse keeps it warm
    lose = mem.get("epi-lose").confidence
    assert CONFIDENCE_FLOOR <= lose < 0.25         # weakened but clamped, stays auditable


def test_apply_advantages_skips_quarantined_and_unknown_memories():
    mem = TieredMemoryStore()
    mem.add(MemoryRecord(memory_id="epi-q", tier="episodic", text="poisoned", confidence=1.0))
    mem.quarantine("epi-q", "contradicted")
    events = [
        *_run("r1", "c1", root="carrier_down", passed=True, cost=0.0, mem_ids=["epi-q", "ghost"]),
        *_run("r2", "c1", root="wrong_key", passed=False, cost=6.0, mem_ids=[]),
    ]
    report = apply_advantages(mem, build_groups(events, _TRUTH))
    assert report["reinforced"] == [] and report["weakened"] == []
    assert mem.get("epi-q").confidence == 1.0      # quarantine is not overridden


# ── end-to-end on the real trace format ──────────────────────────────────────
def test_grpo_over_replayed_real_ledger_credits_the_memory_confirmed_rollout(tmp_path):
    """Cold rollout probes; after consolidation the warm rollout of the SAME case
    uses procedural memory to probe less and freshly confirms episodic memory. Replaying the persisted JSONL ledger
    and grouping by case must give the warm rollout the positive group-relative
    advantage, and applying it must reinforce exactly the recalled memory."""
    ledger_path = tmp_path / "grpo_trace.jsonl"
    orch = build_network_rca_orchestrator(ledger_path, seed_memory=False)
    # This fixture initially exposes three relevant checks; after one verified
    # run procedural memory retains the two checks that actually contributed.
    case = load_seed_cases()[2]
    gt = load_ground_truth()

    orch.diagnose(case)                                            # cold: probes
    consolidate_run(list(orch._run_events), case, orch.memory, orch.skills, orch._last_evidence)
    orch.diagnose(case)                                            # warm: recall

    groups = build_groups(JSONLTraceLedger(ledger_path).replay(), gt)
    group = next(g for g in groups if g.case_id == case.id)
    cold, warm = group.samples
    assert 0 < warm.probes < cold.probes                           # cheaper, never stale-evidence-only
    assert warm.resolved_from_memory and not cold.resolved_from_memory
    assert warm.reward > cold.reward                               # cost penalty bites
    assert warm.advantage > 0 > cold.advantage                     # group-relative

    resolved_id = warm.memory_ids[0]
    before = orch.memory.get(resolved_id).confidence
    report = apply_advantages(orch.memory, [group])
    assert resolved_id in report["reinforced"]
    assert orch.memory.get(resolved_id).confidence > before        # stop-gate parameter moved
