"""Deterministic evaluation for active incident-investigation traces.

The evaluator consumes already-exported structured traces.  It performs no
retrieval, model call, tool call, or synthetic trace generation.  The bundled
fixture is deliberately labelled ``synthetic_controlled`` and exercises the
metric contract; its scores are not evidence of live-system performance.

The important distinction in this benchmark is between a correct guess and a
grounded confirmation.  A confirmation is false when its root cause is wrong
or when it is emitted before all labelled decisive evidence has been retrieved.
This makes withheld-evidence negatives useful without requiring an LLM judge.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = 1
EVALUATOR_VERSION = "autopoiesis-active-investigation-benchmark/1"
VARIANTS = ("direct_api", "bm25", "hybrid_filtered", "full_active")
Variant = Literal["direct_api", "bm25", "hybrid_filtered", "full_active"]
AdversarialKind = Literal[
    "same_symptom_different_root",
    "same_keyword_different_entity",
    "ip_reassignment",
    "stale_configuration",
    "contradictory_logs",
    "tool_timeout_decisive_hidden",
    "combined_fault",
]
REQUIRED_ADVERSARIAL_KINDS = frozenset(
    {
        "same_symptom_different_root",
        "same_keyword_different_entity",
        "ip_reassignment",
        "stale_configuration",
        "contradictory_logs",
        "tool_timeout_decisive_hidden",
        "combined_fault",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _require_unique_nonempty(values: Sequence[str], field: str) -> None:
    if not values or any(not value.strip() for value in values):
        raise ValueError(f"{field} must contain non-empty values")
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must contain unique values")


class EvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    roles: list[Literal["decisive", "contradiction", "supporting", "distractor"]]
    stale: bool = False
    entity_mismatch: bool = False

    @model_validator(mode="after")
    def validate_roles(self) -> "EvidenceItem":
        _require_unique_nonempty(self.roles, "roles")
        return self


class GoldLabels(StrictModel):
    root_causes: list[str]
    decisive_evidence_ids: list[str]
    contradiction_evidence_ids: list[str] = Field(default_factory=list)
    allowed_action_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_labels(self) -> "GoldLabels":
        _require_unique_nonempty(self.root_causes, "root_causes")
        _require_unique_nonempty(self.decisive_evidence_ids, "decisive_evidence_ids")
        for field, values in (
            ("contradiction_evidence_ids", self.contradiction_evidence_ids),
            ("allowed_action_ids", self.allowed_action_ids),
        ):
            if any(not value.strip() for value in values) or len(values) != len(set(values)):
                raise ValueError(f"{field} must contain unique non-empty values")
        return self


class TraceEvent(StrictModel):
    sequence: int = Field(ge=1)
    state_version: int = Field(ge=1)
    process_generation: int = Field(ge=1)
    event_type: Literal["retrieval", "probe", "hypothesis", "action", "restart", "conclusion"]
    evidence_id: str | None = None
    rank: int | None = Field(default=None, ge=1)
    probe_key: str | None = None
    root_causes: list[str] = Field(default_factory=list)
    hypothesis_status: Literal["candidate", "rejected", "confirmed"] | None = None
    action_id: str | None = None
    checkpoint_restored: bool | None = None
    confirmed: bool | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "TraceEvent":
        present = {
            "evidence_id": self.evidence_id is not None,
            "rank": self.rank is not None,
            "probe_key": self.probe_key is not None,
            "root_causes": bool(self.root_causes),
            "hypothesis_status": self.hypothesis_status is not None,
            "action_id": self.action_id is not None,
            "checkpoint_restored": self.checkpoint_restored is not None,
            "confirmed": self.confirmed is not None,
        }
        required = {
            "retrieval": {"evidence_id", "rank"},
            "probe": {"probe_key"},
            "hypothesis": {"root_causes", "hypothesis_status"},
            "action": {"action_id"},
            "restart": {"checkpoint_restored"},
            "conclusion": {"confirmed"},
        }[self.event_type]
        supplied = {key for key, value in present.items() if value}
        if not required.issubset(supplied):
            missing = sorted(required.difference(supplied))
            raise ValueError(f"{self.event_type} event missing fields: {missing}")
        allowed = set(required)
        if self.event_type == "conclusion":
            allowed.add("root_causes")
        unexpected = supplied.difference(allowed)
        if unexpected:
            raise ValueError(f"{self.event_type} event has fields for another event type: {sorted(unexpected)}")
        if self.root_causes:
            _require_unique_nonempty(self.root_causes, "root_causes")
        if self.event_type == "conclusion":
            if self.confirmed and not self.root_causes:
                raise ValueError("confirmed conclusion requires root_causes")
            if self.confirmed is False and self.root_causes:
                raise ValueError("unconfirmed conclusion must not publish root_causes")
        return self


class InvestigationRun(StrictModel):
    variant: Variant
    trace: list[TraceEvent]

    @model_validator(mode="after")
    def validate_trace(self) -> "InvestigationRun":
        if not self.trace:
            raise ValueError("trace must not be empty")
        sequences = [event.sequence for event in self.trace]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("trace sequences must be unique and increasing")
        conclusions = [event for event in self.trace if event.event_type == "conclusion"]
        if len(conclusions) != 1 or self.trace[-1].event_type != "conclusion":
            raise ValueError("trace must end with exactly one conclusion event")
        retrievals = [event for event in self.trace if event.event_type == "retrieval"]
        ranks = [event.rank for event in retrievals]
        evidence_ids = [event.evidence_id for event in retrievals]
        if len(ranks) != len(set(ranks)):
            raise ValueError("retrieval ranks must be unique within a trace")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("retrieved evidence ids must be unique within a trace")
        if self.trace[0].process_generation != 1:
            raise ValueError("a trace must begin at process_generation=1")
        for previous, current in zip(self.trace, self.trace[1:]):
            generation_changed = current.process_generation != previous.process_generation
            if generation_changed and current.event_type != "restart":
                raise ValueError("process generation changes must be represented by a restart event")
            if current.event_type == "restart" and not generation_changed:
                raise ValueError("restart event must advance process_generation")
            if current.process_generation < previous.process_generation:
                raise ValueError("process_generation must not decrease")
        return self


class BenchmarkCase(StrictModel):
    case_id: str = Field(min_length=1)
    adversarial_kinds: list[AdversarialKind]
    withheld_decisive_evidence: bool = False
    evidence_catalog: list[EvidenceItem]
    gold: GoldLabels
    runs: list[InvestigationRun]

    @model_validator(mode="after")
    def validate_case(self) -> "BenchmarkCase":
        _require_unique_nonempty(self.adversarial_kinds, "adversarial_kinds")
        evidence_by_id = {item.evidence_id: item for item in self.evidence_catalog}
        if len(evidence_by_id) != len(self.evidence_catalog):
            raise ValueError(f"{self.case_id}: duplicate evidence_id")
        labelled_ids = set(self.gold.decisive_evidence_ids + self.gold.contradiction_evidence_ids)
        unknown_labels = labelled_ids.difference(evidence_by_id)
        if unknown_labels:
            raise ValueError(f"{self.case_id}: labelled evidence is absent: {sorted(unknown_labels)}")
        for evidence_id in self.gold.decisive_evidence_ids:
            if "decisive" not in evidence_by_id[evidence_id].roles:
                raise ValueError(f"{self.case_id}: {evidence_id} lacks decisive role")
        for evidence_id in self.gold.contradiction_evidence_ids:
            if "contradiction" not in evidence_by_id[evidence_id].roles:
                raise ValueError(f"{self.case_id}: {evidence_id} lacks contradiction role")
        variants = [run.variant for run in self.runs]
        if set(variants) != set(VARIANTS) or len(variants) != len(VARIANTS):
            raise ValueError(f"{self.case_id}: runs must contain each benchmark variant exactly once")
        for run in self.runs:
            retrieved = {
                event.evidence_id
                for event in run.trace
                if event.event_type == "retrieval" and event.evidence_id is not None
            }
            unknown = retrieved.difference(evidence_by_id)
            if unknown:
                raise ValueError(f"{self.case_id}/{run.variant}: unknown evidence ids: {sorted(unknown)}")
            if self.withheld_decisive_evidence:
                leaked = retrieved.intersection(self.gold.decisive_evidence_ids)
                if leaked:
                    raise ValueError(
                        f"{self.case_id}/{run.variant}: withheld decisive evidence leaked: {sorted(leaked)}"
                    )
        if "tool_timeout_decisive_hidden" in self.adversarial_kinds and not self.withheld_decisive_evidence:
            raise ValueError("tool_timeout_decisive_hidden case must withhold decisive evidence")
        return self


class BenchmarkBundle(StrictModel):
    schema_version: Literal[1]
    benchmark_id: str = Field(min_length=1)
    data_classification: Literal["synthetic_controlled"]
    benchmark_purpose: Literal["evaluator_contract_smoke_test"]
    top_k: int = Field(ge=1)
    variants: list[Variant]
    cases: list[BenchmarkCase]

    @model_validator(mode="after")
    def validate_bundle(self) -> "BenchmarkBundle":
        if tuple(self.variants) != VARIANTS:
            raise ValueError(f"variants must appear in canonical order: {list(VARIANTS)}")
        if not self.cases:
            raise ValueError("cases must not be empty")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id must be unique")
        covered = {kind for case in self.cases for kind in case.adversarial_kinds}
        missing = REQUIRED_ADVERSARIAL_KINDS.difference(covered)
        if missing:
            raise ValueError(f"benchmark is missing adversarial kinds: {sorted(missing)}")
        return self


class RunMetrics(StrictModel):
    case_id: str
    variant: Variant
    decisive_evidence_recall_at_k: float | None
    distractor_retrieval_rate: float
    stale_retrieval_rate: float
    entity_mismatch_retrieval_rate: float
    contradiction_recall: float | None
    root_cause_accurate: bool
    false_confirmation: bool
    withheld_evidence_false_confirmation: bool | None
    probe_count: int
    repeated_probe_count: int
    incorrect_action_count: int
    restart_continuity: bool | None


def _exact_roots(observed: Sequence[str], expected: Sequence[str]) -> bool:
    return set(observed) == set(expected) and len(observed) == len(expected)


def _retrieved_before(trace: Sequence[TraceEvent], sequence: int) -> set[str]:
    return {
        event.evidence_id
        for event in trace
        if event.event_type == "retrieval"
        and event.sequence < sequence
        and event.evidence_id is not None
    }


def _confirmation_is_grounded(event: TraceEvent, case: BenchmarkCase, trace: Sequence[TraceEvent]) -> bool:
    if not _exact_roots(event.root_causes, case.gold.root_causes):
        return False
    if case.withheld_decisive_evidence:
        return False
    retrieved = _retrieved_before(trace, event.sequence)
    return set(case.gold.decisive_evidence_ids).issubset(retrieved)


def _restart_continuity(trace: Sequence[TraceEvent]) -> bool | None:
    changes: list[tuple[TraceEvent, TraceEvent]] = []
    for previous, current in zip(trace, trace[1:]):
        if current.process_generation != previous.process_generation:
            changes.append((previous, current))
    if not changes:
        return None
    monotonic = all(
        current.state_version >= previous.state_version
        for previous, current in zip(trace, trace[1:])
    )
    restored = all(
        current.event_type == "restart"
        and current.checkpoint_restored is True
        and current.state_version >= previous.state_version
        for previous, current in changes
    )
    return monotonic and restored


def evaluate_run(case: BenchmarkCase, run: InvestigationRun, *, top_k: int) -> RunMetrics:
    evidence_by_id = {item.evidence_id: item for item in case.evidence_catalog}
    top_retrievals = sorted(
        (
            event
            for event in run.trace
            if event.event_type == "retrieval" and event.rank is not None and event.rank <= top_k
        ),
        key=lambda event: int(event.rank or 0),
    )
    top_ids = {str(event.evidence_id) for event in top_retrievals}
    denominator = len(top_retrievals)

    def selected_rate(predicate: Any) -> float:
        if denominator == 0:
            return 0.0
        selected = sum(1 for evidence_id in top_ids if predicate(evidence_by_id[evidence_id]))
        return round(selected / denominator, 6)

    decisive_recall = None
    if not case.withheld_decisive_evidence:
        decisive_recall = round(
            len(top_ids.intersection(case.gold.decisive_evidence_ids))
            / len(case.gold.decisive_evidence_ids),
            6,
        )
    contradiction_recall = None
    if case.gold.contradiction_evidence_ids:
        contradiction_recall = round(
            len(top_ids.intersection(case.gold.contradiction_evidence_ids))
            / len(case.gold.contradiction_evidence_ids),
            6,
        )

    conclusion = run.trace[-1]
    root_accurate = bool(
        conclusion.confirmed
        and _exact_roots(conclusion.root_causes, case.gold.root_causes)
    )
    confirmation_events = [
        event
        for event in run.trace
        if (event.event_type == "hypothesis" and event.hypothesis_status == "confirmed")
        or (event.event_type == "conclusion" and event.confirmed is True)
    ]
    false_confirmation = any(
        not _confirmation_is_grounded(event, case, run.trace) for event in confirmation_events
    )

    probes = [
        str(event.probe_key) for event in run.trace if event.event_type == "probe" and event.probe_key
    ]
    repeated_probes = sum(count - 1 for count in Counter(probes).values() if count > 1)
    incorrect_actions = 0
    for event in run.trace:
        if event.event_type != "action":
            continue
        prior_grounded_confirmation = any(
            candidate.sequence < event.sequence
            and candidate.event_type == "hypothesis"
            and candidate.hypothesis_status == "confirmed"
            and _confirmation_is_grounded(candidate, case, run.trace)
            for candidate in run.trace
        )
        if event.action_id not in case.gold.allowed_action_ids or not prior_grounded_confirmation:
            incorrect_actions += 1

    return RunMetrics(
        case_id=case.case_id,
        variant=run.variant,
        decisive_evidence_recall_at_k=decisive_recall,
        distractor_retrieval_rate=selected_rate(lambda item: "distractor" in item.roles),
        stale_retrieval_rate=selected_rate(lambda item: item.stale),
        entity_mismatch_retrieval_rate=selected_rate(lambda item: item.entity_mismatch),
        contradiction_recall=contradiction_recall,
        root_cause_accurate=root_accurate,
        false_confirmation=false_confirmation,
        withheld_evidence_false_confirmation=(
            false_confirmation if case.withheld_decisive_evidence else None
        ),
        probe_count=len(probes),
        repeated_probe_count=repeated_probes,
        incorrect_action_count=incorrect_actions,
        restart_continuity=_restart_continuity(run.trace),
    )


def _mean_optional(values: Sequence[float | int | bool | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return round(mean(selected), 6) if selected else None


def _summarize(rows: Sequence[RunMetrics]) -> dict[str, Any]:
    return {
        "run_count": len(rows),
        "decisive_evidence_recall_at_k": _mean_optional(
            [row.decisive_evidence_recall_at_k for row in rows]
        ),
        "distractor_retrieval_rate": _mean_optional(
            [row.distractor_retrieval_rate for row in rows]
        ),
        "stale_retrieval_rate": _mean_optional([row.stale_retrieval_rate for row in rows]),
        "entity_mismatch_retrieval_rate": _mean_optional(
            [row.entity_mismatch_retrieval_rate for row in rows]
        ),
        "contradiction_recall": _mean_optional([row.contradiction_recall for row in rows]),
        "root_cause_accuracy": _mean_optional([row.root_cause_accurate for row in rows]),
        "false_confirmation_rate": _mean_optional([row.false_confirmation for row in rows]),
        "withheld_evidence_false_confirmation_rate": _mean_optional(
            [row.withheld_evidence_false_confirmation for row in rows]
        ),
        "mean_probe_count": _mean_optional([row.probe_count for row in rows]),
        "mean_repeated_probe_count": _mean_optional([row.repeated_probe_count for row in rows]),
        "total_incorrect_actions": sum(row.incorrect_action_count for row in rows),
        "mean_incorrect_actions": _mean_optional([row.incorrect_action_count for row in rows]),
        "restart_continuity_rate": _mean_optional([row.restart_continuity for row in rows]),
    }


PAIR_FIELDS: tuple[tuple[str, str], ...] = (
    ("decisive_evidence_recall_at_k", "decisive_evidence_recall_at_k"),
    ("distractor_retrieval_rate", "distractor_retrieval_rate"),
    ("stale_retrieval_rate", "stale_retrieval_rate"),
    ("entity_mismatch_retrieval_rate", "entity_mismatch_retrieval_rate"),
    ("contradiction_recall", "contradiction_recall"),
    ("root_cause_accuracy", "root_cause_accurate"),
    ("false_confirmation_rate", "false_confirmation"),
    (
        "withheld_evidence_false_confirmation_rate",
        "withheld_evidence_false_confirmation",
    ),
    ("mean_probe_count", "probe_count"),
    ("mean_repeated_probe_count", "repeated_probe_count"),
    ("mean_incorrect_actions", "incorrect_action_count"),
    ("restart_continuity_rate", "restart_continuity"),
)


def _paired_difference(
    baseline: Mapping[str, RunMetrics], candidate: Mapping[str, RunMetrics]
) -> dict[str, Any]:
    if set(baseline) != set(candidate):
        raise ValueError("paired variants must contain identical case ids")
    deltas: dict[str, float | None] = {}
    pair_counts: dict[str, int] = {}
    for report_field, metric_field in PAIR_FIELDS:
        values: list[float] = []
        for case_id in sorted(baseline):
            left = getattr(baseline[case_id], metric_field)
            right = getattr(candidate[case_id], metric_field)
            if left is None or right is None:
                continue
            values.append(float(right) - float(left))
        deltas[f"{report_field}_delta"] = round(mean(values), 6) if values else None
        pair_counts[report_field] = len(values)
    return {
        "pair_count": len(baseline),
        "direction": "candidate_minus_direct_api",
        "metric_pair_counts": pair_counts,
        "deltas": deltas,
    }


def evaluate_active_investigation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and score one complete, paired benchmark bundle."""

    bundle = BenchmarkBundle.model_validate(payload)
    rows: list[RunMetrics] = []
    for case in bundle.cases:
        for variant in VARIANTS:
            run = next(run for run in case.runs if run.variant == variant)
            rows.append(evaluate_run(case, run, top_k=bundle.top_k))

    by_variant = {
        variant: _summarize([row for row in rows if row.variant == variant])
        for variant in VARIANTS
    }
    indexed = {
        variant: {row.case_id: row for row in rows if row.variant == variant}
        for variant in VARIANTS
    }
    paired_differences = {
        f"{variant}_vs_direct_api": _paired_difference(indexed["direct_api"], indexed[variant])
        for variant in VARIANTS
        if variant != "direct_api"
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "benchmark_id": bundle.benchmark_id,
        "data_classification": bundle.data_classification,
        "benchmark_purpose": bundle.benchmark_purpose,
        "scope": (
            "Deterministically scores supplied traces. Synthetic fixture scores validate the "
            "evaluator contract and do not establish live retrieval or diagnosis performance."
        ),
        "top_k": bundle.top_k,
        "case_count": len(bundle.cases),
        "adversarial_coverage": sorted(
            {kind for case in bundle.cases for kind in case.adversarial_kinds}
        ),
        "by_variant": by_variant,
        "paired_differences": paired_differences,
        "runs": [row.model_dump(mode="json") for row in rows],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="structured trace benchmark JSON")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = evaluate_active_investigation(payload)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkBundle",
    "EVALUATOR_VERSION",
    "REQUIRED_ADVERSARIAL_KINDS",
    "RunMetrics",
    "VARIANTS",
    "evaluate_active_investigation",
    "evaluate_run",
]
