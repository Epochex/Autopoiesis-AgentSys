"""Deterministic hypothesis competition for a long-lived investigation.

The model may propose candidates and probes, but this module owns their state.
Historical memory can make a candidate worth testing; only a successful,
current observation for the same entity and incident window can confirm or
reject it.  Failed tool calls remain visible without being mistaken for
counter-evidence.

The aggregate is intentionally small and serializable.  Every mutation creates
a validated snapshot with a larger ``state_version``, so a worker can persist
the snapshot, restart, and continue the same investigation deterministically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.investigate.observation_predicate import ObservationPredicate


HypothesisStatus = Literal["proposed", "testing", "rejected", "confirmed"]
ProbeStatus = Literal["available", "selected", "completed", "failed"]
EvidencePolarity = Literal["supports", "opposes", "neutral"]
CollectionStatus = Literal["observed", "tool_failed"]
EvidenceSource = Literal[
    "telemetry",
    "live_tool",
    "configuration",
    "action_readback",
    "historical_memory",
    "knowledge_document",
    "replay_fixture",
]

# These sources describe this incident directly.  Memory and documents propose
# useful directions, but cannot establish what is true for the current case.
_CURRENT_SOURCES: frozenset[str] = frozenset(
    {
        "telemetry",
        "live_tool",
        "configuration",
        "action_readback",
        "replay_fixture",
    }
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("value must not be empty")
    return value


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RootCauseHypothesis(_StrictModel):
    """One falsifiable cause scoped to an entity and an incident window."""

    hypothesis_id: str
    statement: str
    entity_id: str
    valid_from: datetime
    valid_to: datetime
    status: HypothesisStatus = "proposed"
    supporting_evidence_ids: tuple[str, ...] = ()
    opposing_evidence_ids: tuple[str, ...] = ()
    updated_at: datetime
    updated_in_version: int = Field(default=0, ge=0)
    required_decisive_supports: int = Field(default=1, ge=1, le=8)
    origin: Literal["catalog", "model"] = "catalog"
    archive_eligible: bool = False

    _clean = field_validator("hypothesis_id", "statement", "entity_id")(_non_empty)
    _utc = field_validator("valid_from", "valid_to", "updated_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_window_and_references(self) -> "RootCauseHypothesis":
        if self.valid_to < self.valid_from:
            raise ValueError("hypothesis valid_to precedes valid_from")
        supporting = _unique(self.supporting_evidence_ids)
        opposing = _unique(self.opposing_evidence_ids)
        if set(supporting) & set(opposing):
            raise ValueError("one evidence item cannot both support and oppose a hypothesis")
        object.__setattr__(self, "supporting_evidence_ids", supporting)
        object.__setattr__(self, "opposing_evidence_ids", opposing)
        return self


class ProbeCandidate(_StrictModel):
    """A read-only observation that can separate a set of live candidates."""

    probe_id: str
    description: str
    target_entity_id: str
    distinguishes_hypothesis_ids: tuple[str, ...] = Field(min_length=1)
    priority: int = 0
    estimated_cost: float = Field(default=1.0, ge=0.0)
    status: ProbeStatus = "available"
    selected_in_version: int | None = Field(default=None, ge=1)
    observation_predicate: ObservationPredicate | None = None

    _clean = field_validator("probe_id", "description", "target_entity_id")(_non_empty)

    @model_validator(mode="after")
    def normalize_targets(self) -> "ProbeCandidate":
        targets = _unique(self.distinguishes_hypothesis_ids)
        if not targets:
            raise ValueError("probe must distinguish at least one hypothesis")
        if self.status == "available" and self.selected_in_version is not None:
            raise ValueError("an available probe cannot have selected_in_version")
        if self.status != "available" and self.selected_in_version is None:
            raise ValueError("a used probe requires selected_in_version")
        object.__setattr__(self, "distinguishes_hypothesis_ids", targets)
        return self


class EvidenceInput(_StrictModel):
    """A proposed evidence edge before the aggregate assigns its sequence."""

    evidence_id: str
    hypothesis_id: str
    entity_id: str
    observed_at: datetime
    source: EvidenceSource
    polarity: EvidencePolarity
    decisive: bool = False
    collection_status: CollectionStatus = "observed"
    summary: str
    probe_id: str | None = None
    resolves_evidence_ids: tuple[str, ...] = ()

    _clean = field_validator("evidence_id", "hypothesis_id", "entity_id", "summary")(
        _non_empty
    )
    _utc = field_validator("observed_at")(_aware_utc)

    @field_validator("probe_id")
    @classmethod
    def clean_optional_probe(cls, value: str | None) -> str | None:
        return _non_empty(value) if value is not None else None

    @model_validator(mode="after")
    def normalize_resolution(self) -> "EvidenceInput":
        resolved = _unique(self.resolves_evidence_ids)
        if self.evidence_id in resolved:
            raise ValueError("evidence cannot resolve itself")
        object.__setattr__(self, "resolves_evidence_ids", resolved)
        return self


class EvidenceObservation(EvidenceInput):
    """Evidence stored in the case ledger with a stable sequence number."""

    sequence: int = Field(ge=1)


class HypothesisLoopState(_StrictModel):
    """Complete restorable state of one hypothesis competition."""

    case_id: str
    state_version: int = Field(default=0, ge=0)
    last_evidence_sequence: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    hypotheses: tuple[RootCauseHypothesis, ...] = ()
    probes: tuple[ProbeCandidate, ...] = ()
    evidence: tuple[EvidenceObservation, ...] = ()

    _clean = field_validator("case_id")(_non_empty)
    _utc = field_validator("created_at", "updated_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "HypothesisLoopState":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")

        hypotheses = {item.hypothesis_id: item for item in self.hypotheses}
        if len(hypotheses) != len(self.hypotheses):
            raise ValueError("hypothesis ids must be unique")
        probes = {item.probe_id: item for item in self.probes}
        if len(probes) != len(self.probes):
            raise ValueError("probe ids must be unique")
        evidence = {item.evidence_id: item for item in self.evidence}
        if len(evidence) != len(self.evidence):
            raise ValueError("evidence ids must be unique")

        sequences = [item.sequence for item in self.evidence]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("evidence sequences must be unique and ordered")
        if (max(sequences, default=0)) != self.last_evidence_sequence:
            raise ValueError("last_evidence_sequence does not match stored evidence")

        for probe in self.probes:
            missing = set(probe.distinguishes_hypothesis_ids) - set(hypotheses)
            if missing:
                raise ValueError(f"probe references unknown hypotheses: {sorted(missing)}")
        for item in self.evidence:
            if item.hypothesis_id not in hypotheses:
                raise ValueError(f"evidence references unknown hypothesis: {item.hypothesis_id}")
            if item.probe_id is not None and item.probe_id not in probes:
                raise ValueError(f"evidence references unknown probe: {item.probe_id}")
            for resolved_id in item.resolves_evidence_ids:
                resolved = evidence.get(resolved_id)
                if resolved is None:
                    raise ValueError(f"evidence resolves unknown item: {resolved_id}")
                if resolved.sequence >= item.sequence:
                    raise ValueError("evidence may resolve only an earlier observation")
                if resolved.hypothesis_id != item.hypothesis_id:
                    raise ValueError("evidence may resolve only the same hypothesis")

        for hypothesis in self.hypotheses:
            supporting = {
                item.evidence_id
                for item in self.evidence
                if item.hypothesis_id == hypothesis.hypothesis_id
                and item.collection_status == "observed"
                and item.polarity == "supports"
            }
            opposing = {
                item.evidence_id
                for item in self.evidence
                if item.hypothesis_id == hypothesis.hypothesis_id
                and item.collection_status == "observed"
                and item.polarity == "opposes"
            }
            if set(hypothesis.supporting_evidence_ids) != supporting:
                raise ValueError("hypothesis supporting evidence index is inconsistent")
            if set(hypothesis.opposing_evidence_ids) != opposing:
                raise ValueError("hypothesis opposing evidence index is inconsistent")
            expected = _status_from_observations(hypothesis, self.evidence)
            # Once a probe has been attempted, its target remains under test even
            # if collection failed or only some of a batch's hypotheses yielded
            # observations.
            if hypothesis.status != expected:
                pristine_testing = (
                    hypothesis.status == "testing"
                    and expected == "proposed"
                    and any(
                        probe.status != "available"
                        and hypothesis.hypothesis_id in probe.distinguishes_hypothesis_ids
                        for probe in self.probes
                    )
                )
                if not pristine_testing:
                    raise ValueError(
                        f"hypothesis {hypothesis.hypothesis_id} status is inconsistent "
                        f"with its observations"
                    )
        return self


def _is_current(observation: EvidenceObservation, hypothesis: RootCauseHypothesis) -> bool:
    return (
        observation.collection_status == "observed"
        and observation.source in _CURRENT_SOURCES
        and observation.entity_id == hypothesis.entity_id
        and hypothesis.valid_from <= observation.observed_at <= hypothesis.valid_to
    )


def _status_from_observations(
    hypothesis: RootCauseHypothesis,
    observations: Sequence[EvidenceObservation],
) -> HypothesisStatus:
    relevant = [item for item in observations if item.hypothesis_id == hypothesis.hypothesis_id]
    successful = [item for item in relevant if item.collection_status == "observed"]
    current = [item for item in successful if _is_current(item, hypothesis)]
    resolved_ids = {
        resolved_id
        for item in current
        for resolved_id in item.resolves_evidence_ids
    }
    unresolved = [item for item in current if item.evidence_id not in resolved_ids]
    supports = [item for item in unresolved if item.polarity == "supports"]
    opposition = [item for item in unresolved if item.polarity == "opposes"]
    has_decisive_support = (
        sum(item.decisive for item in supports)
        >= hypothesis.required_decisive_supports
    )
    has_decisive_opposition = any(item.decisive for item in opposition)

    # Any unresolved opposition keeps a supported candidate open.  If both
    # sides claim decisive observations, the contradiction must be investigated.
    if has_decisive_support:
        return "testing" if opposition else "confirmed"
    if has_decisive_opposition:
        return "rejected"
    if successful:
        return "testing"
    return "proposed"


class HypothesisLoop:
    """Stateful facade over immutable, fully serializable snapshots."""

    def __init__(self, state: HypothesisLoopState):
        self._state = HypothesisLoopState.model_validate(state.model_dump())

    @classmethod
    def create(cls, case_id: str, *, at: datetime | None = None) -> "HypothesisLoop":
        timestamp = _aware_utc(at or datetime.now(timezone.utc))
        return cls(
            HypothesisLoopState(
                case_id=case_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    @classmethod
    def restore(cls, payload: str | bytes | dict[str, object]) -> "HypothesisLoop":
        if isinstance(payload, (str, bytes)):
            state = HypothesisLoopState.model_validate_json(payload)
        else:
            state = HypothesisLoopState.model_validate(payload)
        return cls(state)

    @property
    def state(self) -> HypothesisLoopState:
        return self._state

    def dump_json(self) -> str:
        return self._state.model_dump_json()

    def add_hypothesis(self, hypothesis: RootCauseHypothesis) -> RootCauseHypothesis:
        if hypothesis.status != "proposed":
            raise ValueError("a new hypothesis must start as proposed")
        if hypothesis.supporting_evidence_ids or hypothesis.opposing_evidence_ids:
            raise ValueError("a new hypothesis cannot arrive with evidence")
        if self._hypothesis(hypothesis.hypothesis_id, required=False) is not None:
            raise ValueError(f"duplicate hypothesis id: {hypothesis.hypothesis_id}")
        version = self._state.state_version + 1
        stored = hypothesis.model_copy(update={"updated_in_version": version})
        self._commit(
            hypotheses=(*self._state.hypotheses, stored),
            at=stored.updated_at,
        )
        return stored

    def add_probe(self, probe: ProbeCandidate, *, at: datetime | None = None) -> ProbeCandidate:
        if probe.status != "available":
            raise ValueError("a new probe must be available")
        if any(item.probe_id == probe.probe_id for item in self._state.probes):
            raise ValueError(f"duplicate probe id: {probe.probe_id}")
        unknown = set(probe.distinguishes_hypothesis_ids) - {
            item.hypothesis_id for item in self._state.hypotheses
        }
        if unknown:
            raise ValueError(f"probe references unknown hypotheses: {sorted(unknown)}")
        self._commit(
            probes=(*self._state.probes, probe),
            at=_aware_utc(at or datetime.now(timezone.utc)),
        )
        return probe

    def select_next_probe(self, *, at: datetime | None = None) -> ProbeCandidate | None:
        active = {
            item.hypothesis_id
            for item in self._state.hypotheses
            if item.status in {"proposed", "testing"}
        }
        ranked: list[tuple[int, int, float, str, ProbeCandidate]] = []
        for probe in self._state.probes:
            if probe.status != "available":
                continue
            coverage = len(active & set(probe.distinguishes_hypothesis_ids))
            if coverage:
                ranked.append(
                    (-coverage, -probe.priority, probe.estimated_cost, probe.probe_id, probe)
                )
        if not ranked:
            return None

        selected = sorted(ranked, key=lambda item: item[:4])[0][4]
        return self._select_probe(selected, at=at)

    def select_probe(
        self,
        probe_id: str,
        *,
        at: datetime | None = None,
    ) -> ProbeCandidate:
        """Select a named available probe requested by an investigation turn."""
        selected = next(
            (item for item in self._state.probes if item.probe_id == probe_id),
            None,
        )
        if selected is None:
            raise KeyError(probe_id)
        if selected.status != "available":
            raise ValueError(f"probe is not available: {probe_id}")
        active = {
            item.hypothesis_id
            for item in self._state.hypotheses
            if item.status in {"proposed", "testing"}
        }
        if not active.intersection(selected.distinguishes_hypothesis_ids):
            raise ValueError(f"probe has no active hypothesis: {probe_id}")
        return self._select_probe(selected, at=at)

    def _select_probe(
        self,
        selected: ProbeCandidate,
        *,
        at: datetime | None,
    ) -> ProbeCandidate:
        version = self._state.state_version + 1
        used = selected.model_copy(
            update={"status": "selected", "selected_in_version": version}
        )
        probes = tuple(used if item.probe_id == used.probe_id else item for item in self._state.probes)
        timestamp = _aware_utc(at or datetime.now(timezone.utc))
        hypotheses = tuple(
            item.model_copy(
                update={
                    "status": "testing",
                    "updated_at": timestamp,
                    "updated_in_version": version,
                }
            )
            if item.status == "proposed"
            and item.hypothesis_id in used.distinguishes_hypothesis_ids
            else item
            for item in self._state.hypotheses
        )
        self._commit(hypotheses=hypotheses, probes=probes, at=timestamp)
        return used

    def record_evidence(self, item: EvidenceInput) -> EvidenceObservation:
        return self.record_evidence_batch((item,))[0]

    def record_evidence_batch(
        self, items: Sequence[EvidenceInput]
    ) -> tuple[EvidenceObservation, ...]:
        if not items:
            return ()
        existing_ids = {item.evidence_id for item in self._state.evidence}
        incoming_ids = [item.evidence_id for item in items]
        if len(incoming_ids) != len(set(incoming_ids)) or existing_ids & set(incoming_ids):
            raise ValueError("evidence ids must be unique")

        hypotheses_by_id = {item.hypothesis_id: item for item in self._state.hypotheses}
        probes_by_id = {item.probe_id: item for item in self._state.probes}
        known_evidence = {item.evidence_id: item for item in self._state.evidence}
        sequence = self._state.last_evidence_sequence
        stored: list[EvidenceObservation] = []
        for candidate in items:
            if candidate.hypothesis_id not in hypotheses_by_id:
                raise ValueError(f"unknown hypothesis: {candidate.hypothesis_id}")
            if candidate.probe_id is not None and candidate.probe_id not in probes_by_id:
                raise ValueError(f"unknown probe: {candidate.probe_id}")
            for resolved_id in candidate.resolves_evidence_ids:
                resolved = known_evidence.get(resolved_id)
                if resolved is None:
                    raise ValueError(f"cannot resolve unknown evidence: {resolved_id}")
                if resolved.hypothesis_id != candidate.hypothesis_id:
                    raise ValueError("evidence may resolve only the same hypothesis")
            sequence += 1
            observation = EvidenceObservation(**candidate.model_dump(), sequence=sequence)
            stored.append(observation)
            known_evidence[observation.evidence_id] = observation

        all_evidence = (*self._state.evidence, *stored)
        affected_ids = {item.hypothesis_id for item in stored}
        version = self._state.state_version + 1
        latest_at = max(item.observed_at for item in stored)
        hypotheses: list[RootCauseHypothesis] = []
        for hypothesis in self._state.hypotheses:
            if hypothesis.hypothesis_id not in affected_ids:
                hypotheses.append(hypothesis)
                continue
            supporting = tuple(
                item.evidence_id
                for item in all_evidence
                if item.hypothesis_id == hypothesis.hypothesis_id
                and item.collection_status == "observed"
                and item.polarity == "supports"
            )
            opposing = tuple(
                item.evidence_id
                for item in all_evidence
                if item.hypothesis_id == hypothesis.hypothesis_id
                and item.collection_status == "observed"
                and item.polarity == "opposes"
            )
            provisional = hypothesis.model_copy(
                update={
                    "supporting_evidence_ids": supporting,
                    "opposing_evidence_ids": opposing,
                }
            )
            status = _status_from_observations(provisional, all_evidence)
            if status == "proposed" and hypothesis.status == "testing":
                status = "testing"
            hypotheses.append(
                provisional.model_copy(
                    update={
                        "status": status,
                        "updated_at": latest_at,
                        "updated_in_version": version,
                    }
                )
            )

        probe_results: dict[str, list[EvidenceObservation]] = {}
        for observation in stored:
            if observation.probe_id is not None:
                probe_results.setdefault(observation.probe_id, []).append(observation)
        probes = tuple(
            probe.model_copy(
                update={
                    "status": (
                        "completed"
                        if any(
                            item.collection_status == "observed"
                            for item in probe_results[probe.probe_id]
                        )
                        else "failed"
                    ),
                    "selected_in_version": probe.selected_in_version or version,
                }
            )
            if probe.probe_id in probe_results
            else probe
            for probe in self._state.probes
        )
        self._commit(
            hypotheses=tuple(hypotheses),
            probes=probes,
            evidence=all_evidence,
            last_evidence_sequence=sequence,
            at=latest_at,
        )
        return tuple(stored)

    def get_hypothesis(self, hypothesis_id: str) -> RootCauseHypothesis:
        found = self._hypothesis(hypothesis_id, required=True)
        assert found is not None
        return found

    def _hypothesis(
        self, hypothesis_id: str, *, required: bool
    ) -> RootCauseHypothesis | None:
        for item in self._state.hypotheses:
            if item.hypothesis_id == hypothesis_id:
                return item
        if required:
            raise KeyError(hypothesis_id)
        return None

    def _commit(self, *, at: datetime, **updates: object) -> None:
        timestamp = _aware_utc(at)
        payload = self._state.model_dump()
        payload.update(updates)
        payload.update(
            {
                "state_version": self._state.state_version + 1,
                "updated_at": max(timestamp, self._state.updated_at),
            }
        )
        self._state = HypothesisLoopState.model_validate(payload)


__all__ = [
    "CollectionStatus",
    "EvidenceInput",
    "EvidenceObservation",
    "EvidencePolarity",
    "EvidenceSource",
    "HypothesisLoop",
    "HypothesisLoopState",
    "HypothesisStatus",
    "ProbeCandidate",
    "ProbeStatus",
    "RootCauseHypothesis",
]
