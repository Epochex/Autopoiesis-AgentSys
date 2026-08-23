"""Offline, paired memory ablation for the network RCA agent.

The report keeps the experimental unit visible.  Every arm sees the same
``(case_id, seed)`` instance, arm order is shuffled inside that block, and raw
rows are retained so an aggregate can be reconstructed.

The default run is deliberately model-free.  It uses the rule reasoner and
read-only mock tools.  No environment flag can turn a default run into a paid
call; a caller would have to provide a different evaluator in different code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Literal, Mapping, Sequence

from core.context.compiler import ContextCompiler
from core.evolve.consolidate import consolidate_run
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.observability import ExecutionObserver
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.verifier.verifier import Verifier
from domains.network_rca.adapters.mock_device import MockDeviceAdapter
from domains.network_rca.factory import load_ground_truth, load_seed_cases
from domains.network_rca.reasoner import ROOT_CAUSE_EVIDENCE_CONTRACTS, build_diagnosis
from domains.network_rca.skills.network_skills import register_network_rca_skills


POWER_STATEMENT = (
    "功效声明：在记忆臂赢 20%、输 5%（OR=4）的合理不一致分布下，"
    "n=40 功效约 38%，n=60 约 65%；本评测规模检测不出 10pp 以下的效应。"
)

ArmMode = Literal["memory", "empty", "no_memory", "sham", "static_runbook"]
ScenarioType = Literal["ACT", "HOLD-healthy", "HOLD-outofscope"]


@dataclass(frozen=True)
class Arm:
    """One controlled arm.

    ``token_budget`` and ``temperature`` are explicit even though the offline
    reasoner has no sampling.  Keeping them in every row makes a later model
    run auditable without changing the report schema.
    """

    name: str
    mode: ArmMode = "no_memory"
    description: str = ""
    token_budget: int = 2_048
    temperature: float = 0.0
    retrieval_k: int = 6

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("arm name must not be empty")
        if self.mode not in {"memory", "empty", "no_memory", "sham", "static_runbook"}:
            raise ValueError(f"unknown arm mode: {self.mode}")
        if self.token_budget < 1 or self.retrieval_k < 1:
            raise ValueError("token_budget and retrieval_k must be positive")


DEFAULT_ARMS: tuple[Arm, ...] = (
    Arm("M_memory", "memory", "经核实后更新的记忆"),
    Arm("A0_empty", "empty", "不执行，只在预算结束时评分"),
    Arm("A1_no_memory", "no_memory", "相同规则推理器和工具，关闭记忆"),
    Arm("A2_sham_memory", "sham", "等条数、等 token、语义无关的历史条目"),
    Arm("A3_static_runbook", "static_runbook", "固定且不更新的人工 runbook"),
)


@dataclass(frozen=True)
class Scenario:
    case: Any
    case_id: str
    seed: int
    scenario_type: ScenarioType
    source_case_id: str
    alert_signature: str
    perturbation: str | None = None

    @property
    def pair_id(self) -> str:
        return f"{self.case_id}:{self.seed}"


@dataclass
class CaseResult:
    case_id: str
    seed: int
    pair_id: str
    scenario_type: ScenarioType
    source_case_id: str
    alert_signature: str
    perturbation: str | None
    arm: str
    arm_mode: ArmMode
    order_position: int
    execution_order: list[str]
    reasoner: str
    token_budget: int
    temperature: float
    attempted: bool
    verifier_passed: bool
    root_cause_correct: bool
    verified_repair: bool
    root_cause_key: str | None
    state_changes: int
    unnecessary_changes: int
    wall_clock_ms: float
    context_tokens: int
    context_memory_tokens: int
    tool_calls: int
    retrieved_memory_ids: list[str]
    budget_exhausted: bool
    capability_eligible: bool = False
    memory_injection: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AblationReport:
    power_statement: str
    design: dict[str, Any]
    metrics: dict[str, Any]
    arms: list[dict[str, Any]]
    raw_results: dict[str, list[CaseResult]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "power_statement": self.power_statement,
            "design": self.design,
            "metrics": self.metrics,
            "arms": self.arms,
            "raw_results": {
                name: [row.to_dict() for row in rows]
                for name, rows in self.raw_results.items()
            },
        }


_NETWORK_ROOT = Path(__file__).resolve().parents[2] / "domains" / "network_rca"
_MOCK_RESPONSES = _NETWORK_ROOT / "fixtures" / "mock_device_responses.json"
_WORD = re.compile(r"[a-z0-9_]+")
_SHAM_OVERLAP_THRESHOLD = 0.15
_BOOTSTRAP_SAMPLES = 2_000

# These are old incidents from unrelated service families.  They are shuffled
# deterministically, filtered before use, and then resized.  Resizing changes
# length only; it never borrows words, assets, or labels from the target case.
_IRRELEVANT_HISTORY = (
    ("hist-database", "archived database checkpoint lag during a reporting batch"),
    ("hist-certificate", "archived certificate rotation for an internal signing service"),
    ("hist-storage", "archived storage snapshot retention review after a backup window"),
    ("hist-printer", "archived printer queue saturation in a remote office"),
    ("hist-payroll", "archived payroll export delayed by a calendar lock"),
    ("hist-cooling", "archived cooling sensor calibration in a laboratory cabinet"),
    ("hist-index", "archived search index compaction after a document import"),
    ("hist-battery", "archived battery inspection for a handheld scanner fleet"),
)

_STATIC_RUNBOOK = (
    "Start from the alert payload and record its time source affected asset service "
    "impact and reporter. Confirm that identifiers refer to the same device and time "
    "window before joining observations. Collect fresh read only evidence for link "
    "state interface counters routes policy decisions dependency health resource "
    "pressure and recent configuration history. Mark missing evidence and keep alternative causes "
    "open until the required checks agree. Compare counters as deltas over a stated "
    "window because cumulative values alone do not establish an active fault. Check "
    "both ends of a dependency and distinguish a reachable control plane from a "
    "working data path. For routing or access failures inspect connected routes next "
    "hop reachability policy matching address objects translation rules and service "
    "ports. For interface alarms confirm administrative state carrier peer state "
    "driver health and hardware error counters before proposing replacement. For "
    "security or license alarms separate inspection coverage from basic forwarding. "
    "State the selected cause the current evidence ids that support it and any facts "
    "that would overturn it. Classify ownership before action. Healthy transient "
    "conditions stay under observation. Provider owned or otherwise out of scope "
    "faults are escalated with evidence and no local modification. For an in scope "
    "fault prepare the smallest reversible change and identify its expected state "
    "transition rollback trigger and approval owner. Never infer permission from an "
    "alert. Execute only after explicit human approval through an authorized change "
    "path. After a change repeat the original probes and verify the user visible symptom plus the "
    "supporting device state. Stop when verification fails or new evidence conflicts "
    "with the diagnosis."
)


class _ScenarioAdapter:
    """Keep the alert fixed while perturbing post-alert observations for HOLD."""

    def __init__(self) -> None:
        self._base = MockDeviceAdapter(_MOCK_RESPONSES)
        self.scenario_type: ScenarioType = "ACT"

    def set_scenario(self, scenario_type: ScenarioType) -> None:
        self.scenario_type = scenario_type

    def query(self, case_id: str, operation: str) -> list[dict]:
        rows = json.loads(json.dumps(self._base.query(case_id, operation)))
        if self.scenario_type == "ACT":
            return rows
        if self.scenario_type == "HOLD-healthy":
            for row in rows:
                row["summary"] = (
                    "Fresh post-alert observation is healthy; the alert came from a "
                    "transient benign anomaly and no state change is warranted."
                )
                row["data"] = {
                    "healthy": True,
                    "transient_alert": True,
                    "state_change_warranted": False,
                }
            return rows
        for row in rows:
            row["summary"] = (
                f"{row.get('summary', '')} The affected component is provider-owned; "
                "local state changes are out of scope and escalation is required."
            )
            data = dict(row.get("data") or {})
            data.update(
                {
                    "owner": "external_provider",
                    "local_change_allowed": False,
                    "escalation_required": True,
                }
            )
            row["data"] = data
        return rows


class _FixedRetrievalStore(TieredMemoryStore):
    """Return an already-audited retrieval plan without doing a second ranking."""

    def __init__(
        self,
        plan: Mapping[str, Sequence[MemoryRecord]],
        *,
        diagnostics: Sequence[dict[str, Any]] = (),
    ) -> None:
        super().__init__(enabled=True)
        self._plan = {
            tier: [record.model_copy(deep=True) for record in records]
            for tier, records in plan.items()
        }
        unique: dict[str, MemoryRecord] = {}
        for records in self._plan.values():
            for record in records:
                unique[record.memory_id] = record
        self.seed(list(unique.values()))
        self._fixed_diagnostics = [dict(item) for item in diagnostics]

    def retrieve(
        self,
        query_terms: list[str],
        asset_ids: list[str],
        limit_per_tier: int = 3,
        **_: Any,
    ) -> dict[str, list[MemoryRecord]]:
        del query_terms, asset_ids
        tiers = ("episodic", "semantic", "procedural", "asset_profile")
        return {
            tier: [record.model_copy(deep=True) for record in self._plan.get(tier, ())][
                :limit_per_tier
            ]
            for tier in tiers
        }

    def retrieval_diagnostics(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._fixed_diagnostics]


def _stable_seed(*parts: Any) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _alert_signature(case: Any) -> str:
    payload = {
        "query": case.query,
        "query_terms": list(case.query_terms),
        "assets": list(case.assets),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_scenarios(
    cases: Sequence[Any], *, repeats: int = 3, seed: int = 20_260_822
) -> list[Scenario]:
    """Build a paired stream with one third HOLD cases when the size permits.

    HOLD scenarios keep the original alert payload and case id.  Only the
    post-alert tool observations are perturbed by ``_ScenarioAdapter``.  This
    prevents a classifier from winning by spotting a synthetic query template.
    """

    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if not cases:
        return []

    keys = [(case_index, repeat_index) for case_index in range(len(cases)) for repeat_index in range(repeats)]
    wanted = round(len(keys) / 3)
    rng = random.Random(_stable_seed(seed, "hold-allocation"))
    candidates = list(keys)
    rng.shuffle(candidates)

    # With repeated cases, leave at least one ACT instance in each family.  It
    # supplies the independent capability check used by the SABER-style gate.
    per_case_hold: dict[int, int] = {index: 0 for index in range(len(cases))}
    max_per_case = max(0, repeats - 1)
    selected: list[tuple[int, int]] = []
    for key in candidates:
        case_index, _ = key
        if len(selected) >= wanted:
            break
        if per_case_hold[case_index] >= max_per_case:
            continue
        selected.append(key)
        per_case_hold[case_index] += 1
    selected_set = set(selected)
    hold_kind = {
        key: ("HOLD-healthy" if index % 2 == 0 else "HOLD-outofscope")
        for index, key in enumerate(selected)
    }

    scenarios: list[Scenario] = []
    for case_index, case in enumerate(cases):
        signature = _alert_signature(case)
        for repeat_index in range(repeats):
            pair_seed = seed + repeat_index
            key = (case_index, repeat_index)
            scenario_type: ScenarioType = hold_kind.get(key, "ACT")  # type: ignore[assignment]
            perturbation = None
            if key in selected_set:
                perturbation = (
                    "post-alert observations changed to a healthy state"
                    if scenario_type == "HOLD-healthy"
                    else "fault ownership changed to an external provider"
                )
            scenarios.append(
                Scenario(
                    case=case,
                    case_id=case.id,
                    seed=pair_seed,
                    scenario_type=scenario_type,
                    source_case_id=case.id,
                    alert_signature=signature,
                    perturbation=perturbation,
                )
            )
    return scenarios


def _build_orchestrator(
    root: Path,
    arm: Arm,
    memory: TieredMemoryStore,
) -> tuple[SingleAgentRCAOrchestrator, _ScenarioAdapter]:
    adapter = _ScenarioAdapter()
    registry = SkillRegistry()
    register_network_rca_skills(registry, adapter)
    observer = ExecutionObserver(root / f"{arm.name}.observability.jsonl")
    orchestrator = SingleAgentRCAOrchestrator(
        memory=memory,
        context_compiler=ContextCompiler(
            token_budget=arm.token_budget,
            enabled=True,
            strategy="structured",
            max_memory_lines=24,
            max_evidence_lines=32,
        ),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=True, top_k=3),
        verifier=Verifier(enabled=True, evidence_contracts=ROOT_CAUSE_EVIDENCE_CONTRACTS),
        diagnosis_builder=build_diagnosis,
        ledger_path=root / f"{arm.name}.jsonl",
        observer=observer,
        trace_durable=False,
    )
    return orchestrator, adapter


def _empty_plan() -> dict[str, list[MemoryRecord]]:
    return {tier: [] for tier in ("episodic", "semantic", "procedural", "asset_profile")}


def _treatment_plan(
    memory: TieredMemoryStore, case: Any, retrieval_k: int
) -> dict[str, list[MemoryRecord]]:
    plan = memory.retrieve(
        list(case.query_terms),
        list(case.assets),
        limit_per_tier=retrieval_k,
        graph_depth=2,
        graph_candidate_limit=48,
    )
    return {
        tier: [record.model_copy(deep=True) for record in records]
        for tier, records in plan.items()
    }


def _memory_line(record: MemoryRecord) -> str:
    timeline: list[str] = []
    if record.first_observed_at is not None:
        timeline.append(f"first={record.first_observed_at.isoformat()}")
    if record.last_observed_at is not None:
        timeline.append(f"last={record.last_observed_at.isoformat()}")
    if record.event_type:
        timeline.append(f"event={record.event_type}")
    if record.relations:
        timeline.append(
            "relations="
            + ",".join(
                f"{relation.relation_type}:{relation.target_id}"
                for relation in record.relations[:8]
            )
        )
    if record.source_trace_ids:
        timeline.append("traces=" + ",".join(record.source_trace_ids[-3:]))
    prefix = f" [{' '.join(timeline)}]" if timeline else ""
    return f"{record.tier}{prefix}: {record.text}"


def _token_count(text: str) -> int:
    return ContextCompiler._default_token_counter(text)


def _hashed_embedding(text: str, dimensions: int = 512) -> list[float]:
    vector = [0.0] * dimensions
    for token in _WORD.findall(text.lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest, "big") % dimensions
        vector[slot] += 1.0
    return vector


def _semantic_overlap(left: str, right: str) -> float:
    a = _hashed_embedding(left)
    b = _hashed_embedding(right)
    dot = sum(x * y for x, y in zip(a, b))
    a_norm = math.sqrt(sum(x * x for x in a))
    b_norm = math.sqrt(sum(x * x for x in b))
    return dot / (a_norm * b_norm) if a_norm and b_norm else 0.0


def _resized_irrelevant_text(
    source: str, desired_line_tokens: int, *, empty_line_tokens: int
) -> str:
    content_tokens = max(1, desired_line_tokens - empty_line_tokens)
    source_words = _WORD.findall(source.lower()) or ["archived"]
    neutral = source_words + ["archive", "calibration", "record", "closed"]
    words = [neutral[index % len(neutral)] for index in range(content_tokens)]
    return " ".join(words)


def _make_sham_plan(
    target: Mapping[str, Sequence[MemoryRecord]], case: Any, seed: int
) -> tuple[dict[str, list[MemoryRecord]], dict[str, Any], list[dict[str, Any]]]:
    query_text = " ".join([case.query, *case.query_terms, *case.assets])
    eligible = [
        (source_id, text, _semantic_overlap(query_text, text))
        for source_id, text in _IRRELEVANT_HISTORY
        if _semantic_overlap(query_text, text) <= _SHAM_OVERLAP_THRESHOLD
    ]
    if not eligible:
        raise RuntimeError(f"no sham history passed the overlap threshold for {case.id}")
    rng = random.Random(_stable_seed(seed, case.id, "sham"))
    rng.shuffle(eligible)

    plan = _empty_plan()
    diagnostics: list[dict[str, Any]] = []
    source_ids: list[str] = []
    overlaps: list[float] = []
    flat_targets = [
        record
        for tier in ("episodic", "semantic", "procedural", "asset_profile")
        for record in target.get(tier, ())
    ]
    target_to_sham: dict[str, MemoryRecord] = {}
    source_by_sham: dict[str, tuple[str, str]] = {}
    for index, record in enumerate(flat_targets):
        source_id, source_text, _ = eligible[index % len(eligible)]
        sham = MemoryRecord(
            memory_id=f"sham-{case.id}-{seed}-{index}",
            tier=record.tier,
            text="archive",
            tags=["sham", "historical", source_id],
            asset_ids=[f"unrelated-{index}"],
            confidence=record.confidence,
            first_observed_at=record.first_observed_at,
            last_observed_at=record.last_observed_at,
            event_type="archived_event" if record.event_type else None,
        )
        plan[record.tier].append(sham)
        target_to_sham[record.memory_id] = sham
        source_by_sham[sham.memory_id] = (source_id, source_text)

    # A temporal edge produces a Current State line in the compiler. Copying
    # only the edge shape keeps that line's position and size controlled while
    # all ids, assets, event labels, and prose remain unrelated to the case.
    for record in flat_targets:
        sham = target_to_sham[record.memory_id]
        sham.relations = [
            relation.model_copy(
                deep=True,
                update={"target_id": target_to_sham[relation.target_id].memory_id},
            )
            for relation in record.relations
            if relation.target_id in target_to_sham
        ]

    target_tokens = 0
    sham_tokens = 0
    for index, record in enumerate(flat_targets):
        sham = target_to_sham[record.memory_id]
        source_id, source_text = source_by_sham[sham.memory_id]
        desired = _token_count(_memory_line(record))
        sham.text = ""
        empty_line_tokens = _token_count(_memory_line(sham))
        if empty_line_tokens >= desired:
            # This would mean structural metadata alone is longer than the
            # complete target line, so exact matching is impossible without
            # changing format. Surface it instead of silently trimming shape.
            raise RuntimeError(
                f"sham structural metadata exceeds target token length for {record.memory_id}"
            )
        sham.text = _resized_irrelevant_text(
            source_text,
            desired,
            empty_line_tokens=empty_line_tokens,
        )
        overlap = _semantic_overlap(query_text, sham.text)
        if overlap > _SHAM_OVERLAP_THRESHOLD:
            raise RuntimeError(
                f"resized sham history exceeded overlap threshold for {case.id}: {overlap:.3f}"
            )
        source_ids.append(source_id)
        overlaps.append(overlap)
        target_tokens += desired
        sham_line_tokens = _token_count(_memory_line(sham))
        sham_tokens += sham_line_tokens
        diagnostics.append(
            {
                "memory_id": sham.memory_id,
                "tier": sham.tier,
                "source_history_id": source_id,
                "semantic_overlap": round(overlap, 6),
                "overlap_threshold": _SHAM_OVERLAP_THRESHOLD,
                "target_line_tokens": desired,
                "sham_line_tokens": sham_line_tokens,
            }
        )
    audit = {
        "method": "deterministic hashed-token embedding, threshold filter, seeded shuffle",
        "retrieved_count": len(flat_targets),
        "target_retrieved_count": len(flat_targets),
        "injected_line_tokens": sham_tokens,
        "target_line_tokens": target_tokens,
        "token_difference_ratio": (
            abs(sham_tokens - target_tokens) / target_tokens if target_tokens else 0.0
        ),
        "overlap_threshold": _SHAM_OVERLAP_THRESHOLD,
        "max_semantic_overlap": max(overlaps, default=0.0),
        "source_history_ids": source_ids,
    }
    return plan, audit, diagnostics


def _static_plan() -> dict[str, list[MemoryRecord]]:
    plan = _empty_plan()
    plan["procedural"] = [
        MemoryRecord(
            memory_id="static-runbook-v1",
            tier="procedural",
            text=_STATIC_RUNBOOK,
            tags=["fixed", "human-authored", "runbook"],
            asset_ids=[],
            confidence=1.0,
        )
    ]
    return plan


def _event(orch: SingleAgentRCAOrchestrator, kind: str) -> Any | None:
    return next((item for item in orch._run_events if item.kind == kind), None)


def _context_numbers(orch: SingleAgentRCAOrchestrator) -> tuple[int, int, list[str]]:
    event = _event(orch, "context_compiled")
    if event is None:
        return 0, 0, []
    payload = event.payload
    memory_tokens = 0
    for section in payload.get("sections", []):
        for item in section.get("kept", []):
            if item.get("kind") in {"episodic", "semantic", "procedural", "asset_profile"}:
                memory_tokens += int(item.get("estimated_tokens", 0))
    return (
        int(payload.get("estimated_tokens_after", 0)),
        memory_tokens,
        list(payload.get("included_memory_ids", [])),
    )


def _state_change_counts(orch: SingleAgentRCAOrchestrator, scenario: Scenario) -> tuple[int, int]:
    changes = [
        event
        for event in orch._run_events
        if event.kind in {"state_changed", "change_applied"}
        or (event.kind == "tool_called" and event.payload.get("readonly") is False)
    ]
    # HOLD labels come from the oracle, so every applied change there is
    # unnecessary. ACT changes require an explicit event-level marker to call a
    # change unnecessary; absence of that marker is not silently judged.
    unnecessary = (
        len(changes)
        if scenario.scenario_type != "ACT"
        else sum(bool(event.payload.get("unnecessary")) for event in changes)
    )
    return len(changes), unnecessary


def _run_one(
    scenario: Scenario,
    arm: Arm,
    order_position: int,
    execution_order: list[str],
    orchestrator: SingleAgentRCAOrchestrator | None,
    adapter: _ScenarioAdapter | None,
    truth: Any | None,
    *,
    injection: Mapping[str, Any] | None = None,
) -> CaseResult:
    if arm.mode == "empty":
        return CaseResult(
            case_id=scenario.case_id,
            seed=scenario.seed,
            pair_id=scenario.pair_id,
            scenario_type=scenario.scenario_type,
            source_case_id=scenario.source_case_id,
            alert_signature=scenario.alert_signature,
            perturbation=scenario.perturbation,
            arm=arm.name,
            arm_mode=arm.mode,
            order_position=order_position,
            execution_order=execution_order,
            reasoner="none",
            token_budget=arm.token_budget,
            temperature=arm.temperature,
            attempted=False,
            verifier_passed=False,
            root_cause_correct=False,
            verified_repair=False,
            root_cause_key=None,
            state_changes=0,
            unnecessary_changes=0,
            wall_clock_ms=0.0,
            context_tokens=0,
            context_memory_tokens=0,
            tool_calls=0,
            retrieved_memory_ids=[],
            budget_exhausted=True,
            memory_injection=dict(injection or {}),
        )

    assert orchestrator is not None and adapter is not None
    adapter.set_scenario(scenario.scenario_type)
    started = time.perf_counter_ns()
    diagnosis, verification = orchestrator.diagnose(
        scenario.case,
        run_id=f"ablation-{scenario.seed}-{arm.name}-{scenario.case_id}",
    )
    wall_clock_ms = (time.perf_counter_ns() - started) / 1_000_000
    context_tokens, memory_tokens, retrieved = _context_numbers(orchestrator)
    tool_calls = sum(
        event.kind == "tool_called" and not event.payload.get("blocked")
        for event in orchestrator._run_events
    )
    changes, unnecessary = _state_change_counts(orchestrator, scenario)
    correct = bool(
        truth is not None
        and diagnosis.root_cause_key == truth.expected_root_cause_key
        and scenario.scenario_type == "ACT"
    )
    verified_repair = bool(scenario.scenario_type == "ACT" and correct and verification.passed)
    return CaseResult(
        case_id=scenario.case_id,
        seed=scenario.seed,
        pair_id=scenario.pair_id,
        scenario_type=scenario.scenario_type,
        source_case_id=scenario.source_case_id,
        alert_signature=scenario.alert_signature,
        perturbation=scenario.perturbation,
        arm=arm.name,
        arm_mode=arm.mode,
        order_position=order_position,
        execution_order=execution_order,
        reasoner="rule",
        token_budget=arm.token_budget,
        temperature=arm.temperature,
        attempted=tool_calls > 0,
        verifier_passed=bool(verification.passed),
        root_cause_correct=correct,
        verified_repair=verified_repair,
        root_cause_key=diagnosis.root_cause_key,
        state_changes=changes,
        unnecessary_changes=unnecessary,
        wall_clock_ms=round(wall_clock_ms, 3),
        context_tokens=context_tokens,
        context_memory_tokens=memory_tokens,
        tool_calls=tool_calls,
        retrieved_memory_ids=retrieved,
        budget_exhausted=False,
        memory_injection=dict(injection or {}),
    )


def _binomial_probability(n: int, k: int) -> float:
    return math.comb(n, k) * (0.5**n)


def mcnemar_mid_p(b: int, c: int) -> float:
    """Two-sided McNemar mid-p under the conditional Binomial(n, 0.5)."""

    if b < 0 or c < 0:
        raise ValueError("discordant counts must be nonnegative")
    discordant = b + c
    if discordant == 0:
        return 1.0
    tail = min(b, c)
    lower_strict = sum(_binomial_probability(discordant, value) for value in range(tail))
    p_value = 2.0 * (lower_strict + 0.5 * _binomial_probability(discordant, tail))
    return min(1.0, p_value)


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile needs at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def paired_bootstrap_ci(
    differences: Sequence[float], *, seed: int, samples: int = _BOOTSTRAP_SAMPLES
) -> list[float] | None:
    if not differences:
        return None
    if samples < 100:
        raise ValueError("paired bootstrap needs at least 100 samples")
    rng = random.Random(seed)
    n = len(differences)
    estimates = [
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(samples)
    ]
    return [round(_percentile(estimates, 0.025), 6), round(_percentile(estimates, 0.975), 6)]


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = ((index + 1) + end) / 2.0
        for original, _ in ordered[index:end]:
            ranks[original] = average
        index = end
    return ranks


def _subset_sum_distribution(ranks: Sequence[float]) -> dict[int, int]:
    # Doubled ranks are integers even when a tie receives a half rank.
    distribution = {0: 1}
    for rank in ranks:
        step = int(round(rank * 2))
        updated = dict(distribution)
        for total, count in distribution.items():
            updated[total + step] = updated.get(total + step, 0) + count
        distribution = updated
    return distribution


def hodges_lehmann_wilcoxon(differences: Sequence[float]) -> dict[str, Any]:
    """Paired HL pseudomedian and a 95% signed-rank inversion interval."""

    values = [float(value) for value in differences]
    if not values:
        return {
            "hodges_lehmann": None,
            "wilcoxon_95_ci": None,
            "wilcoxon_p_two_sided": None,
            "effective_n": 0,
            "note": "no paired differences",
        }
    walsh = sorted((values[i] + values[j]) / 2.0 for i in range(len(values)) for j in range(i, len(values)))
    estimate = statistics.median(walsh)
    nonzero = [value for value in values if value != 0.0]
    if not nonzero:
        return {
            "hodges_lehmann": 0.0,
            "wilcoxon_95_ci": [0.0, 0.0],
            "wilcoxon_p_two_sided": 1.0,
            "effective_n": 0,
            "note": "all paired differences are zero",
        }

    ranks = _average_ranks([abs(value) for value in nonzero])
    positive = sum(rank for rank, value in zip(ranks, nonzero) if value > 0)
    distribution = _subset_sum_distribution(ranks)
    total_assignments = 2 ** len(nonzero)
    observed = int(round(positive * 2))
    lower = sum(count for score, count in distribution.items() if score <= observed)
    upper = sum(count for score, count in distribution.items() if score >= observed)
    p_value = min(1.0, 2.0 * min(lower, upper) / total_assignments)

    cumulative = 0
    critical_doubled: int | None = None
    for score in sorted(distribution):
        next_cumulative = cumulative + distribution[score]
        if next_cumulative / total_assignments <= 0.025:
            critical_doubled = score
            cumulative = next_cumulative
        else:
            break
    interval: list[float] | None = None
    note = "exact signed-rank inversion; zeros removed and ties receive average ranks"
    if critical_doubled is not None:
        critical = math.floor(critical_doubled / 2)
        lower_index = min(critical, len(walsh) - 1)
        upper_index = max(0, len(walsh) - critical - 1)
        interval = [round(walsh[lower_index], 6), round(walsh[upper_index], 6)]
    else:
        note += "; this effective sample cannot support a finite exact 95% interval"
    return {
        "hodges_lehmann": round(estimate, 6),
        "wilcoxon_95_ci": interval,
        "wilcoxon_p_two_sided": round(p_value, 6),
        "effective_n": len(nonzero),
        "note": note,
    }


def _paired_rows(
    raw: Mapping[str, Sequence[CaseResult]], treatment: str, baseline: str
) -> list[tuple[CaseResult, CaseResult]]:
    left = {(row.case_id, row.seed): row for row in raw[treatment]}
    right = {(row.case_id, row.seed): row for row in raw[baseline]}
    keys = sorted(set(left) & set(right))
    return [(left[key], right[key]) for key in keys]


def _m1(
    raw: Mapping[str, Sequence[CaseResult]], treatment: str, baselines: Sequence[str], seed: int
) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for baseline in baselines:
        pairs = [
            pair
            for pair in _paired_rows(raw, treatment, baseline)
            if pair[0].scenario_type == "ACT" and pair[1].scenario_type == "ACT"
        ]
        a = sum(memory.verified_repair and base.verified_repair for memory, base in pairs)
        b = sum(memory.verified_repair and not base.verified_repair for memory, base in pairs)
        c = sum(not memory.verified_repair and base.verified_repair for memory, base in pairs)
        d = sum(not memory.verified_repair and not base.verified_repair for memory, base in pairs)
        diffs = [float(memory.verified_repair) - float(base.verified_repair) for memory, base in pairs]
        comparisons[baseline] = {
            "paired_n": len(pairs),
            "table_2x2": {
                "columns": ["baseline_success", "baseline_failure"],
                "rows": ["memory_success", "memory_failure"],
                "cells": [[a, b], [c, d]],
                "a_both_success": a,
                "b_memory_only": b,
                "c_baseline_only": c,
                "d_both_failure": d,
            },
            "discordant_pairs": b + c,
            "memory_rate": round((a + b) / len(pairs), 6) if pairs else None,
            "baseline_rate": round((a + c) / len(pairs), 6) if pairs else None,
            "paired_difference": round(sum(diffs) / len(diffs), 6) if diffs else None,
            "paired_bootstrap_95_ci": paired_bootstrap_ci(
                diffs, seed=_stable_seed(seed, treatment, baseline, "m1")
            ),
            "mcnemar_mid_p_two_sided": round(mcnemar_mid_p(b, c), 6),
            "method_note": (
                "McNemar mid-p two-sided. The full table is primary; b and c carry the paired information. "
                "The confidence interval resamples paired instances."
            ),
        }
    return {
        "name": "M1 成对核实修复率",
        "primary_comparison": next((name for name in baselines if "A2" in name), None),
        "comparisons": comparisons,
    }


def _m2(
    raw: Mapping[str, Sequence[CaseResult]], treatment: str, baselines: Sequence[str]
) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    metrics = {
        "state_changes": "primary",
        "unnecessary_changes": "primary",
        "wall_clock_ms": "secondary",
        "context_tokens": "secondary",
    }
    for baseline in baselines:
        pairs = [
            pair
            for pair in _paired_rows(raw, treatment, baseline)
            if pair[0].scenario_type == "ACT"
            and pair[0].verified_repair
            and pair[1].verified_repair
        ]
        estimates: dict[str, Any] = {}
        for attribute, priority in metrics.items():
            differences = [
                float(getattr(memory, attribute)) - float(getattr(base, attribute))
                for memory, base in pairs
            ]
            estimate = hodges_lehmann_wilcoxon(differences)
            estimate["priority"] = priority
            estimate["direction"] = "memory minus baseline"
            estimates[attribute] = estimate
        comparisons[baseline] = {
            "both_success_subset_size_S": len(pairs),
            "interpretation": (
                "descriptive only because |S| < 10"
                if len(pairs) < 10
                else "inferential summary"
            ),
            "metrics": estimates,
        }
    return {
        "name": "M2 已解决实例上的修复成本",
        "estimand": "only instances where both arms have a verified repair",
        "primary_comparison": next((name for name in baselines if "A2" in name), None),
        "comparisons": comparisons,
    }


def _apply_capability_gate(raw: Mapping[str, Sequence[CaseResult]]) -> None:
    for rows in raw.values():
        demonstrated = {
            row.source_case_id
            for row in rows
            if row.scenario_type == "ACT" and row.verified_repair and row.attempted
        }
        for row in rows:
            row.capability_eligible = row.source_case_id in demonstrated


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _m3(raw: Mapping[str, Sequence[CaseResult]]) -> dict[str, Any]:
    by_arm: dict[str, Any] = {}
    for arm, rows in raw.items():
        eligible = [row for row in rows if row.capability_eligible]
        holds = [row for row in eligible if row.scenario_type != "ACT"]
        acts = [row for row in eligible if row.scenario_type == "ACT"]
        hold_changed = sum(row.state_changes > 0 for row in holds)
        act_attempted = sum(row.attempted for row in acts)
        changed_scenarios = sum(row.state_changes > 0 for row in eligible)
        verified = sum(row.verified_repair for row in eligible)
        by_arm[arm] = {
            "capability_eligible_scenarios": len(eligible),
            "excluded_incapable_scenarios": len(rows) - len(eligible),
            "hold_scenarios": len(holds),
            "act_scenarios": len(acts),
            "FIR": _ratio(hold_changed, len(holds)),
            "COV": _ratio(act_attempted, len(acts)),
            "SRP": _ratio(verified, changed_scenarios),
            "counts": {
                "hold_with_state_change": hold_changed,
                "hold_total": len(holds),
                "act_attempted": act_attempted,
                "act_total": len(acts),
                "verified_repairs": verified,
                "scenarios_with_state_change": changed_scenarios,
            },
            "undefined_note": (
                "SRP is undefined because this read-only harness emitted no state-change events."
                if changed_scenarios == 0
                else None
            ),
        }
    scenario_rows: list[dict[str, Any]] = []
    pair_ids = sorted({row.pair_id for rows in raw.values() for row in rows})
    for pair_id in pair_ids:
        members = [row for rows in raw.values() for row in rows if row.pair_id == pair_id]
        if not members:
            continue
        scenario_rows.append(
            {
                "pair_id": pair_id,
                "case_id": members[0].case_id,
                "seed": members[0].seed,
                "scenario_type": members[0].scenario_type,
                "alert_signature": members[0].alert_signature,
                "arms": {
                    row.arm: {
                        "capability_eligible": row.capability_eligible,
                        "attempted": row.attempted,
                        "state_changes": row.state_changes,
                        "verified_repair": row.verified_repair,
                    }
                    for row in members
                },
            }
        )
    return {
        "name": "M3 诱饵下的克制度",
        "metrics_must_be_read_together": ["FIR", "COV", "SRP"],
        "capability_gate": (
            "A case family is eligible only after that arm completed and verified an ACT instance "
            "from the same source family. Inaction caused by inability receives no credit."
        ),
        "by_arm": by_arm,
        "paired_scenarios": scenario_rows,
    }


def _token_balance(
    raw: Mapping[str, Sequence[CaseResult]], treatment: str, sham: str
) -> dict[str, Any]:
    pairs = _paired_rows(raw, treatment, sham)
    rows = []
    for memory, fake in pairs:
        denominator = max(memory.context_tokens, 1)
        difference = abs(fake.context_tokens - memory.context_tokens) / denominator
        rows.append(
            {
                "pair_id": memory.pair_id,
                "memory_context_tokens": memory.context_tokens,
                "sham_context_tokens": fake.context_tokens,
                "relative_difference": round(difference, 6),
                "within_10_percent": difference <= 0.10,
                "retrieved_count_equal": len(memory.retrieved_memory_ids)
                == len(fake.retrieved_memory_ids),
            }
        )
    return {
        "comparison": f"{treatment} vs {sham}",
        "pairs": rows,
        "all_within_10_percent": all(row["within_10_percent"] for row in rows),
        "all_retrieved_counts_equal": all(row["retrieved_count_equal"] for row in rows),
        "max_relative_difference": max((row["relative_difference"] for row in rows), default=0.0),
    }


def run_ablation(
    cases: Sequence[Any] | None = None,
    *,
    arms: Sequence[Arm] | None = None,
    repeats: int = 3,
    seed: int = 20_260_822,
) -> AblationReport:
    """Run the complete paired ablation without an LLM call."""

    selected_cases = list(cases) if cases is not None else load_seed_cases()
    selected_arms = list(arms) if arms is not None else list(DEFAULT_ARMS)
    if len({arm.name for arm in selected_arms}) != len(selected_arms):
        raise ValueError("arm names must be unique")
    by_mode = {arm.mode: arm for arm in selected_arms}
    required = {"memory", "empty", "no_memory", "sham", "static_runbook"}
    missing = sorted(required - set(by_mode))
    if missing:
        raise ValueError(f"the controlled design requires all five modes; missing {missing}")
    if len({arm.token_budget for arm in selected_arms if arm.mode != "empty"}) != 1:
        raise ValueError("all executing arms must use the same token budget")
    if len({arm.temperature for arm in selected_arms if arm.mode != "empty"}) != 1:
        raise ValueError("all executing arms must use the same temperature")

    truth_by_id = load_ground_truth()
    scenarios = build_scenarios(selected_cases, repeats=repeats, seed=seed)
    raw: dict[str, list[CaseResult]] = {arm.name: [] for arm in selected_arms}
    treatment_arm = by_mode["memory"]
    sham_arm = by_mode["sham"]

    with TemporaryDirectory(prefix="memory-ablation-") as temporary:
        root = Path(temporary)
        treatment_memory = TieredMemoryStore(enabled=True)
        orchestrators: dict[str, SingleAgentRCAOrchestrator] = {}
        adapters: dict[str, _ScenarioAdapter] = {}
        for arm in selected_arms:
            if arm.mode == "empty":
                continue
            if arm.mode == "memory":
                memory = treatment_memory
            elif arm.mode == "no_memory":
                memory = TieredMemoryStore(enabled=False)
            elif arm.mode == "static_runbook":
                memory = _FixedRetrievalStore(_static_plan())
            else:
                memory = _FixedRetrievalStore(_empty_plan())
            orchestrators[arm.name], adapters[arm.name] = _build_orchestrator(root, arm, memory)

        for scenario in scenarios:
            frozen_treatment = _treatment_plan(
                treatment_memory, scenario.case, treatment_arm.retrieval_k
            )
            sham_plan, sham_audit, sham_diagnostics = _make_sham_plan(
                frozen_treatment, scenario.case, scenario.seed
            )
            sham_store = _FixedRetrievalStore(sham_plan, diagnostics=sham_diagnostics)
            orchestrators[sham_arm.name].memory = sham_store

            order = list(selected_arms)
            random.Random(_stable_seed(seed, scenario.pair_id, "arm-order")).shuffle(order)
            order_names = [arm.name for arm in order]
            truth = truth_by_id.get(scenario.case_id)
            for position, arm in enumerate(order):
                injection: dict[str, Any] = {}
                if arm.mode == "sham":
                    injection = sham_audit
                elif arm.mode == "static_runbook":
                    injection = {
                        "document_id": "static-runbook-v1",
                        "document_sha256": hashlib.sha256(_STATIC_RUNBOOK.encode("utf-8")).hexdigest(),
                        "updated_during_run": False,
                    }
                row = _run_one(
                    scenario,
                    arm,
                    position,
                    order_names,
                    orchestrators.get(arm.name),
                    adapters.get(arm.name),
                    truth,
                    injection=injection,
                )
                raw[arm.name].append(row)
                if arm.mode == "memory" and scenario.scenario_type == "ACT":
                    orch = orchestrators[arm.name]
                    consolidate_run(
                        list(orch._run_events),
                        scenario.case,
                        treatment_memory,
                        orch.skills,
                        list(orch._last_evidence),
                    )

    _apply_capability_gate(raw)
    treatment_name = treatment_arm.name
    baseline_names = [arm.name for arm in selected_arms if arm.mode != "memory"]
    hold_count = sum(scenario.scenario_type != "ACT" for scenario in scenarios)
    hold_share = hold_count / len(scenarios) if scenarios else 0.0
    token_balance = _token_balance(raw, treatment_name, sham_arm.name)
    treatment_memory_tokens = [row.context_memory_tokens for row in raw[treatment_name]]
    static_name = by_mode["static_runbook"].name
    static_memory_tokens = [row.context_memory_tokens for row in raw[static_name]]
    treatment_mean_tokens = (
        sum(treatment_memory_tokens) / len(treatment_memory_tokens)
        if treatment_memory_tokens
        else 0.0
    )
    static_mean_tokens = (
        sum(static_memory_tokens) / len(static_memory_tokens)
        if static_memory_tokens
        else 0.0
    )
    no_memory_name = by_mode["no_memory"].name
    negative_pairs = [
        pair
        for pair in _paired_rows(raw, sham_arm.name, no_memory_name)
        if pair[0].scenario_type == "ACT"
    ]
    negative_context_differences = [
        float(sham.context_tokens - no_memory.context_tokens)
        for sham, no_memory in negative_pairs
    ]
    negative_m1 = _m1(raw, sham_arm.name, [no_memory_name], seed)["comparisons"][
        no_memory_name
    ]
    metrics = {
        "M1": _m1(raw, treatment_name, baseline_names, seed),
        "M2": _m2(raw, treatment_name, baseline_names),
        "M3": _m3(raw),
        "A2_vs_A1_negative_control": {
            "name": "A1 到 A2 的纯上下文体积效应",
            "paired_n": len(negative_pairs),
            "context_tokens_A2_minus_A1": hodges_lehmann_wilcoxon(
                negative_context_differences
            ),
            "verified_repair_table_2x2": negative_m1["table_2x2"],
            "verified_repair_mcnemar_mid_p_two_sided": negative_m1[
                "mcnemar_mid_p_two_sided"
            ],
        },
    }
    design = {
        "mode": "offline_rule_reasoner",
        "llm_calls": 0,
        "pairing_key": ["case_id", "seed"],
        "arm_order": "seeded shuffle within each paired instance",
        "primary_baseline": sham_arm.name,
        "cases": len(selected_cases),
        "repeats": repeats,
        "paired_instances": len(scenarios),
        "hold_instances": hold_count,
        "hold_share": round(hold_share, 6),
        "hold_target_met": not scenarios or 0.30 <= hold_share <= 0.40,
        "hold_construction": (
            "same query, query_terms, assets, and case id as the source ACT case; "
            "only post-alert observations or ownership are perturbed"
        ),
        "sham_memory_balance": token_balance,
        "static_runbook_balance": {
            "comparison": f"{treatment_name} study-period mean vs {static_name}",
            "memory_mean_context_tokens": round(treatment_mean_tokens, 3),
            "static_mean_context_tokens": round(static_mean_tokens, 3),
            "relative_difference": round(
                abs(static_mean_tokens - treatment_mean_tokens) / treatment_mean_tokens,
                6,
            )
            if treatment_mean_tokens
            else None,
            "document_updated_during_run": False,
        },
        "A1_A2_control_note": (
            "A1 and A2 use the same token budget and scaffold. Their realized context length is intentionally "
            "different when A2 injects sham history; that difference estimates context-volume effect. "
            "Token matching is therefore tested between the memory arm and A2."
        ),
        "limitations": [
            "The bundled cases exercise a deterministic read-only diagnosis path; state-change metrics can be structurally zero.",
            "Wall-clock measurements include local file and observer overhead and are secondary.",
            "Context tokens are the compiler's deterministic estimate because the offline rule reasoner has no API usage record.",
            "Any M2 comparison with |S| < 10 is descriptive.",
        ],
    }
    return AblationReport(
        power_statement=POWER_STATEMENT,
        design=design,
        metrics=metrics,
        arms=[asdict(arm) for arm in selected_arms],
        raw_results=raw,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline memory ablation")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20_260_822)
    args = parser.parse_args(argv)
    report = run_ablation(repeats=args.repeats, seed=args.seed)
    # The power boundary is printed before JSON so it cannot be buried below a
    # favorable result.  The same sentence also lives inside the JSON payload.
    print(report.power_statement)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
