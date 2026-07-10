"""GRPO-style group-relative credit assignment over replayable trajectories.

Offline learning signal (Shao et al., 2024 — DeepSeekMath, arXiv:2402.03300):
the replayed trajectories of the *same case* form one group; each trajectory
gets a scalar reward from its own trace events (correctness, verification,
tool cost — real signals only), and its advantage is the group-relative,
std-normalized excess over the group mean::

    advantage_i = (reward_i − mean(group)) / (std(group) + ε)

The advantages are then applied to the exact policy parameters this
architecture exposes: each memory record's ``confidence``, which both ranks
retrieval (``TieredMemoryStore.retrieve``) and gates the orchestrator's
stop-early paths (episodic recall requires confidence ≥ 0.9, the procedural
skill shortcut ≥ 1.4). Trajectories that beat their group baseline reinforce
the memories they read; below-baseline trajectories weaken them — group-
relative credit assignment for the retrieval *and* stopping policy.

Honesty contract: this is deterministic, rule-based policy-parameter
optimization over replayed traces. It is NOT gradient training of an LLM —
GPU-side GRPO fine-tuning remains roadmap, as stated in docs/RESUME.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Mapping, Protocol, Sequence

from core.memory.store import TieredMemoryStore
from core.trace.events import TraceEvent

# ── reward weights (documented, real-signal only) ────────────────────────────
REWARD_CORRECT = 1.0        # diagnosis root_cause_key matches ground truth
REWARD_VERIFIED = 0.5       # verifier confirmed every citation was observed
COST_PENALTY = 0.02         # per unit of observed tool cost — efficiency pressure

ADVANTAGE_EPS = 1e-8        # numerical floor for the std normalizer

# ── policy-update bounds ─────────────────────────────────────────────────────
DEFAULT_LEARNING_RATE = 0.2
CONFIDENCE_FLOOR = 0.2      # a weakened memory stays visible for audit, near-unretrievable
CONFIDENCE_CAP = 3.0        # matches the consolidation conf_cap


class GroundTruthLike(Protocol):
    """The ground-truth attribute reward computation needs (structural)."""

    expected_root_cause_key: str


@dataclass(frozen=True)
class TrajectorySample:
    """One replayed rollout of a case, reduced to its learning signals."""

    run_id: str
    case_id: str
    reward: float
    tool_cost: float
    probes: int
    resolved_from_memory: bool
    memory_ids: tuple[str, ...]     # read-set: the memories this rollout retrieved
    advantage: float = 0.0


@dataclass(frozen=True)
class TrajectoryGroup:
    """All replayed rollouts of one case — the GRPO sampling unit."""

    case_id: str
    samples: tuple[TrajectorySample, ...]


def trajectory_reward(events: Sequence[TraceEvent], expected_root_cause_key: str) -> float:
    """Scalar reward for one rollout, derived only from its persisted trace:
    +1.0 correct root cause, +0.5 verifier pass, −0.02 per unit tool cost."""
    reward = 0.0
    for event in events:
        if event.kind == "diagnosis_completed":
            if event.payload.get("root_cause_key") == expected_root_cause_key:
                reward += REWARD_CORRECT
        elif event.kind == "verifier_result":
            if event.payload.get("passed") is True:
                reward += REWARD_VERIFIED
        elif event.kind == "cost_observed":
            reward -= COST_PENALTY * float(event.payload.get("tool_cost", 0.0))
    return round(reward, 6)


def group_advantages(rewards: Sequence[float], *, normalize: bool = True) -> list[float]:
    """GRPO advantages: reward minus the group-mean baseline, optionally divided
    by the group std (+ε).

    Numerically safe by construction: an empty group returns ``[]``; a singleton
    or uniform group centers to exact zeros (no NaN/inf possible). Advantages
    always sum to ~0 — the group IS the baseline.
    """
    n = len(rewards)
    if n == 0:
        return []
    mean = sum(rewards) / n
    centered = [r - mean for r in rewards]
    if not normalize:
        return centered
    std = math.sqrt(sum(c * c for c in centered) / n)
    return [c / (std + ADVANTAGE_EPS) for c in centered]


def build_groups(
    events: Sequence[TraceEvent],
    ground_truth: Mapping[str, GroundTruthLike],
) -> list[TrajectoryGroup]:
    """Partition a flat replayed ledger into per-case trajectory groups.

    Events are split into rollouts by ``run_id`` (first-seen order preserved,
    so a replayed JSONL ledger reconstructs the original run sequence), grouped
    by ``case_id``, and each sample gets its reward and group-relative
    advantage. Cases without ground truth are skipped — no truth, no reward.
    """
    runs: dict[str, list[TraceEvent]] = {}
    for event in events:
        runs.setdefault(event.run_id, []).append(event)

    by_case: dict[str, list[TrajectorySample]] = {}
    for run_events in runs.values():
        case_id = run_events[0].case_id
        truth = ground_truth.get(case_id)
        if truth is None:
            continue
        by_case.setdefault(case_id, []).append(_sample(run_events, truth))

    groups: list[TrajectoryGroup] = []
    for case_id, samples in by_case.items():
        advantages = group_advantages([s.reward for s in samples])
        groups.append(
            TrajectoryGroup(
                case_id=case_id,
                samples=tuple(
                    replace(s, advantage=round(adv, 6))
                    for s, adv in zip(samples, advantages)
                ),
            )
        )
    return groups


def apply_advantages(
    memory: TieredMemoryStore,
    groups: Sequence[TrajectoryGroup],
    *,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    conf_floor: float = CONFIDENCE_FLOOR,
    conf_cap: float = CONFIDENCE_CAP,
) -> dict[str, object]:
    """Turn group-relative advantages into bounded policy-parameter updates.

    Every memory a rollout read moves by ``learning_rate × advantage`` in
    confidence, clamped to [conf_floor, conf_cap]. Confidence is the parameter
    that ranks retrieval and gates the stop-early/shortcut paths, so above-
    baseline rollouts make their memories easier to recall and act on, and
    below-baseline rollouts make theirs harder. Returns an audit report of the
    ids reinforced/weakened.
    """
    if learning_rate <= 0:
        raise ValueError(f"learning_rate must be > 0, got {learning_rate}")
    reinforced: list[str] = []
    weakened: list[str] = []
    for group in groups:
        for sample in group.samples:
            if sample.advantage == 0.0:
                continue
            for memory_id in sample.memory_ids:
                record = memory.get(memory_id)
                if record is None or record.quarantined:
                    continue
                delta = learning_rate * sample.advantage
                record.confidence = min(conf_cap, max(conf_floor, record.confidence + delta))
                if sample.advantage > 0:
                    record.strength = 1.0          # profitable reuse keeps it warm
                    reinforced.append(memory_id)
                else:
                    weakened.append(memory_id)
    return {
        "groups": len(groups),
        "samples": sum(len(g.samples) for g in groups),
        "reinforced": reinforced,
        "weakened": weakened,
    }


def _sample(run_events: list[TraceEvent], truth: GroundTruthLike) -> TrajectorySample:
    """Reduce one rollout's events to a TrajectorySample (advantage filled later)."""
    memory_ids: list[str] = []
    tool_cost = 0.0
    probes = 0
    resolved = False
    for event in run_events:
        if event.kind == "memory_read":
            for ids in event.payload.values():
                memory_ids.extend(str(mid) for mid in ids)
        elif event.kind == "memory_resolved":
            resolved = True
            mid = event.payload.get("memory_id")
            if mid:
                memory_ids.append(str(mid))
        elif event.kind == "tool_called" and not event.payload.get("blocked"):
            probes += 1
        elif event.kind == "cost_observed":
            tool_cost += float(event.payload.get("tool_cost", 0.0))
    return TrajectorySample(
        run_id=run_events[0].run_id,
        case_id=run_events[0].case_id,
        reward=trajectory_reward(run_events, truth.expected_root_cause_key),
        tool_cost=round(tool_cost, 6),
        probes=probes,
        resolved_from_memory=resolved,
        memory_ids=tuple(dict.fromkeys(memory_ids)),
    )
