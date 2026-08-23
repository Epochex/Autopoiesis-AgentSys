"""Auditable incident dossiers for long-lived network operations.

The dossier is the business record of one fault investigation.  It keeps
evidence references, hypotheses, action receipts, readback, and the observation
window together without turning a detector label into a confirmed root cause.
The aggregate is immutable; callers create a new snapshot and ingest it through
``DossierStore``.  A store must reject illegal state movement and removal of
already accepted evidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SourceMode = Literal["live", "replay", "drill"]
DossierStatus = Literal[
    "open",
    "investigating",
    "mitigating",
    "observing",
    "resolved",
    "escalated",
    "closed_false_positive",
]

_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"investigating", "escalated", "closed_false_positive"}),
    "investigating": frozenset(
        {"mitigating", "observing", "resolved", "escalated", "closed_false_positive"}
    ),
    "mitigating": frozenset({"observing", "resolved", "escalated"}),
    "observing": frozenset({"mitigating", "resolved", "escalated"}),
    "escalated": frozenset({"investigating", "mitigating", "observing"}),
    "resolved": frozenset(),
    "closed_false_positive": frozenset(),
}

_HYPOTHESIS_TRANSITIONS: dict[str, frozenset[str]] = {
    "hypothesis": frozenset({"supported", "refuted"}),
    "supported": frozenset({"confirmed", "refuted"}),
    # New evidence may reopen a discarded candidate or overturn a confirmation.
    "refuted": frozenset({"hypothesis"}),
    "confirmed": frozenset({"refuted"}),
}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("value must not be empty")
    return value


class _DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReference(_DomainModel):
    evidence_id: str
    source_type: Literal[
        "telemetry",
        "operator",
        "configuration",
        "action_receipt",
        "readback",
        "replay_fixture",
    ]
    locator: str
    observed_at: datetime
    summary: str
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _clean_id = field_validator("evidence_id", "locator", "summary")(_non_empty)
    _utc = field_validator("observed_at")(_aware_utc)


class RootCauseHypothesis(_DomainModel):
    hypothesis_id: str
    statement: str
    status: Literal["hypothesis", "supported", "refuted", "confirmed"] = "hypothesis"
    origin: Literal["operator", "analysis", "detector"]
    detector_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: tuple[str, ...] = ()
    updated_at: datetime
    confirmed_by: str | None = None

    _clean = field_validator("hypothesis_id", "statement")(_non_empty)
    _utc = field_validator("updated_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_confirmation(self) -> "RootCauseHypothesis":
        if self.origin == "detector" and not (self.detector_id or "").strip():
            raise ValueError("detector-origin hypothesis requires detector_id")
        if self.status in {"supported", "refuted", "confirmed"} and not self.evidence_ids:
            raise ValueError(f"{self.status} root cause requires evidence")
        if self.status == "confirmed":
            if self.origin == "detector":
                raise ValueError("a detector-origin hypothesis cannot be confirmed directly")
            if not (self.confirmed_by or "").strip():
                raise ValueError("confirmed root cause requires confirmed_by")
        elif self.confirmed_by is not None:
            raise ValueError("confirmed_by is valid only for a confirmed root cause")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        return self


class ReadbackCheck(_DomainModel):
    name: str
    passed: bool
    observed_value: str | int | float | bool | None = None
    evidence_id: str

    _clean = field_validator("name", "evidence_id")(_non_empty)


class ReadbackResult(_DomainModel):
    readback_id: str
    collected_at: datetime
    verdict: Literal["passed", "failed", "partial"]
    checks: tuple[ReadbackCheck, ...] = Field(min_length=1)

    _clean = field_validator("readback_id")(_non_empty)
    _utc = field_validator("collected_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_checks(self) -> "ReadbackResult":
        if self.verdict == "passed" and any(not check.passed for check in self.checks):
            raise ValueError("passed readback requires every check to pass")
        if self.verdict == "failed" and all(check.passed for check in self.checks):
            raise ValueError("failed readback requires a failed check")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("readback check names must be unique")
        object.__setattr__(self, "checks", tuple(sorted(self.checks, key=lambda item: item.name)))
        return self


class ActionReceipt(_DomainModel):
    receipt_id: str
    executor: str
    started_at: datetime
    completed_at: datetime
    outcome: Literal["succeeded", "failed", "declined", "rolled_back", "interrupted"]
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    external_operation_id: str | None = None

    _clean = field_validator("receipt_id", "executor")(_non_empty)
    _utc = field_validator("started_at", "completed_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_interval(self) -> "ActionReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("action receipt completes before it starts")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        return self


class ObservationWindow(_DomainModel):
    started_at: datetime
    planned_end_at: datetime
    verdict: Literal["pending", "passed", "failed", "inconclusive"] = "pending"
    completed_at: datetime | None = None
    hold_duration_seconds: float | None = Field(default=None, ge=0.0)
    evidence_ids: tuple[str, ...] = ()

    _utc_required = field_validator("started_at", "planned_end_at")(_aware_utc)

    @field_validator("completed_at")
    @classmethod
    def normalize_completed(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_window(self) -> "ObservationWindow":
        if self.planned_end_at < self.started_at:
            raise ValueError("observation planned_end_at precedes started_at")
        if self.verdict == "pending":
            if self.completed_at is not None or self.hold_duration_seconds is not None:
                raise ValueError("pending observation cannot have a completion or hold duration")
        else:
            if self.completed_at is None or self.hold_duration_seconds is None:
                raise ValueError("completed observation requires completed_at and hold duration")
            if self.completed_at < self.started_at:
                raise ValueError("observation completes before it starts")
            elapsed = (self.completed_at - self.started_at).total_seconds()
            if abs(elapsed - self.hold_duration_seconds) > 1.0:
                raise ValueError("hold duration must match the observation interval")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        return self


class RemediationAttempt(_DomainModel):
    attempt_id: str
    action: str
    target_asset_id: str
    initiated_at: datetime
    outcome: Literal["pending", "succeeded", "failed", "declined", "rolled_back", "interrupted"]
    precondition_evidence_ids: tuple[str, ...] = ()
    receipt: ActionReceipt | None = None
    readbacks: tuple[ReadbackResult, ...] = ()
    observation: ObservationWindow | None = None

    _clean = field_validator("attempt_id", "action", "target_asset_id")(_non_empty)
    _utc = field_validator("initiated_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_result(self) -> "RemediationAttempt":
        if self.outcome != "pending" and self.receipt is None:
            raise ValueError("completed remediation attempt requires an action receipt")
        if self.receipt is not None and self.receipt.outcome != self.outcome:
            raise ValueError("attempt outcome must match its action receipt")
        if self.receipt is not None and self.receipt.started_at < self.initiated_at:
            raise ValueError("action receipt starts before the attempt")
        ids = [item.readback_id for item in self.readbacks]
        if len(ids) != len(set(ids)):
            raise ValueError("readback ids must be unique within an attempt")
        object.__setattr__(
            self, "precondition_evidence_ids", tuple(sorted(set(self.precondition_evidence_ids)))
        )
        object.__setattr__(self, "readbacks", tuple(sorted(self.readbacks, key=lambda item: item.readback_id)))
        return self


class RecurrenceRelation(_DomainModel):
    prior_dossier_id: str
    detected_at: datetime
    similarity: float = Field(ge=0.0, le=1.0)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    relation_type: Literal["recurrence_of"] = "recurrence_of"

    _clean = field_validator("prior_dossier_id")(_non_empty)
    _utc = field_validator("detected_at")(_aware_utc)

    @model_validator(mode="after")
    def sort_evidence(self) -> "RecurrenceRelation":
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        return self


class IncidentDossier(_DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    dossier_id: str
    source_mode: SourceMode
    status: DossierStatus = "open"
    fault_family: str
    fault_summary: str
    severity: Literal["low", "medium", "high", "critical"]
    symptom_fingerprint: str
    asset_ids: tuple[str, ...] = Field(min_length=1)
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    root_causes: tuple[RootCauseHypothesis, ...] = ()
    remediation_attempts: tuple[RemediationAttempt, ...] = ()
    recurrences: tuple[RecurrenceRelation, ...] = ()

    _clean = field_validator(
        "dossier_id", "fault_family", "fault_summary", "symptom_fingerprint"
    )(_non_empty)
    _utc = field_validator("opened_at", "updated_at")(_aware_utc)

    @field_validator("closed_at")
    @classmethod
    def normalize_closed(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_aggregate(self) -> "IncidentDossier":
        if self.updated_at < self.opened_at:
            raise ValueError("updated_at precedes opened_at")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValueError("closed_at precedes opened_at")
        if self.status in {"resolved", "closed_false_positive"}:
            if self.closed_at is None:
                raise ValueError("closed dossier status requires closed_at")
        elif self.closed_at is not None:
            raise ValueError("only a closed dossier status may set closed_at")

        asset_ids = tuple(sorted(set(map(_non_empty, self.asset_ids))))
        if not asset_ids:
            raise ValueError("dossier requires at least one asset")
        object.__setattr__(self, "asset_ids", asset_ids)

        collections: tuple[tuple[str, Sequence[Any]], ...] = (
            ("evidence", self.evidence),
            ("root cause", self.root_causes),
            ("remediation attempt", self.remediation_attempts),
        )
        id_names = ("evidence_id", "hypothesis_id", "attempt_id")
        for (label, values), id_name in zip(collections, id_names):
            ids = [getattr(value, id_name) for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} ids must be unique")

        evidence_ids = {item.evidence_id for item in self.evidence}
        referenced: set[str] = set()
        for hypothesis in self.root_causes:
            referenced.update(hypothesis.evidence_ids)
        for attempt in self.remediation_attempts:
            referenced.update(attempt.precondition_evidence_ids)
            if attempt.receipt:
                referenced.update(attempt.receipt.evidence_ids)
            for readback in attempt.readbacks:
                referenced.update(check.evidence_id for check in readback.checks)
            if attempt.observation:
                referenced.update(attempt.observation.evidence_ids)
        for recurrence in self.recurrences:
            referenced.update(recurrence.evidence_ids)
        unknown = sorted(referenced - evidence_ids)
        if unknown:
            raise ValueError("unknown evidence references: " + ", ".join(unknown))

        if self.status == "resolved":
            passed_attempts = [
                attempt
                for attempt in self.remediation_attempts
                if attempt.outcome == "succeeded"
                and any(readback.verdict == "passed" for readback in attempt.readbacks)
                and attempt.observation is not None
                and attempt.observation.verdict == "passed"
            ]
            if not passed_attempts:
                raise ValueError(
                    "resolved dossier requires a successful action, passed readback, and passed observation"
                )

        object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda item: item.evidence_id)))
        object.__setattr__(
            self, "root_causes", tuple(sorted(self.root_causes, key=lambda item: item.hypothesis_id))
        )
        object.__setattr__(
            self,
            "remediation_attempts",
            tuple(sorted(self.remediation_attempts, key=lambda item: item.attempt_id)),
        )
        object.__setattr__(
            self, "recurrences", tuple(sorted(self.recurrences, key=lambda item: item.prior_dossier_id))
        )
        return self

    def canonical_json(self) -> str:
        """Return stable JSON for hashing, replay, and idempotent ingestion."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def transition_to(self, status: DossierStatus, *, at: datetime) -> "IncidentDossier":
        validate_dossier_transition(self.status, status)
        at = _aware_utc(at)
        updates: dict[str, Any] = {"status": status, "updated_at": at}
        if status in {"resolved", "closed_false_positive"}:
            updates["closed_at"] = at
        payload = self.model_dump()
        payload.update(updates)
        return IncidentDossier.model_validate(payload)


def validate_dossier_transition(previous: DossierStatus, current: DossierStatus) -> None:
    if previous == current:
        return
    if current not in _STATUS_TRANSITIONS[previous]:
        raise ValueError(f"illegal dossier status transition: {previous} -> {current}")


class DossierConflictError(RuntimeError):
    """An accepted dossier snapshot was changed in a non-append-only way."""


class DossierWrite(_DomainModel):
    dossier_id: str
    version: int = Field(ge=1)
    written: bool
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DossierStore(Protocol):
    def ingest(self, dossier: IncidentDossier) -> DossierWrite: ...
    def get(self, dossier_id: str) -> IncidentDossier | None: ...
    def list(self, *, status: DossierStatus | None = None) -> list[IncidentDossier]: ...
    def search(
        self,
        *,
        asset_id: str | None = None,
        fault_family: str | None = None,
        symptom_fingerprint: str | None = None,
        limit: int = 50,
    ) -> list[IncidentDossier]: ...


def _by_id(values: Sequence[Any], name: str) -> dict[str, Any]:
    return {getattr(value, name): value for value in values}


def _validate_update(previous: IncidentDossier, current: IncidentDossier) -> None:
    validate_dossier_transition(previous.status, current.status)
    if current.updated_at < previous.updated_at:
        raise DossierConflictError("dossier updated_at cannot move backwards")
    immutable = ("source_mode", "fault_family", "symptom_fingerprint", "opened_at")
    changed = [name for name in immutable if getattr(previous, name) != getattr(current, name)]
    if changed:
        raise DossierConflictError("immutable dossier fields changed: " + ", ".join(changed))
    if not set(previous.asset_ids).issubset(current.asset_ids):
        raise DossierConflictError("accepted assets cannot be removed")

    previous_evidence = _by_id(previous.evidence, "evidence_id")
    current_evidence = _by_id(current.evidence, "evidence_id")
    for evidence_id, evidence in previous_evidence.items():
        if current_evidence.get(evidence_id) != evidence:
            raise DossierConflictError(f"accepted evidence changed or was removed: {evidence_id}")

    prior_hypotheses = _by_id(previous.root_causes, "hypothesis_id")
    next_hypotheses = _by_id(current.root_causes, "hypothesis_id")
    for hypothesis_id, old in prior_hypotheses.items():
        new = next_hypotheses.get(hypothesis_id)
        if new is None:
            raise DossierConflictError(f"accepted root-cause hypothesis was removed: {hypothesis_id}")
        if (old.statement, old.origin, old.detector_id) != (
            new.statement,
            new.origin,
            new.detector_id,
        ):
            raise DossierConflictError(f"root-cause hypothesis identity changed: {hypothesis_id}")
        if old.status != new.status and new.status not in _HYPOTHESIS_TRANSITIONS[old.status]:
            raise DossierConflictError(
                f"illegal root-cause status transition: {old.status} -> {new.status}"
            )

    for name, id_name in (("remediation_attempts", "attempt_id"), ("recurrences", "prior_dossier_id")):
        old_ids = set(_by_id(getattr(previous, name), id_name))
        new_ids = set(_by_id(getattr(current, name), id_name))
        if not old_ids.issubset(new_ids):
            raise DossierConflictError(f"accepted {name} entries cannot be removed")


class InMemoryDossierStore:
    """Thread-safe store suitable for one long-lived gateway process.

    Re-ingesting byte-equivalent business content is a no-op.  A changed
    snapshot increments its version only after aggregate transition and
    append-only checks pass.
    """

    def __init__(self, dossiers: Sequence[IncidentDossier] = ()) -> None:
        self._rows: dict[str, tuple[IncidentDossier, int]] = {}
        self._lock = RLock()
        for dossier in dossiers:
            self.ingest(dossier)

    def ingest(self, dossier: IncidentDossier) -> DossierWrite:
        with self._lock:
            current = self._rows.get(dossier.dossier_id)
            if current is None:
                version = 1
            else:
                previous, version = current
                if previous.content_digest == dossier.content_digest:
                    return DossierWrite(
                        dossier_id=dossier.dossier_id,
                        version=version,
                        written=False,
                        content_digest=dossier.content_digest,
                    )
                _validate_update(previous, dossier)
                version += 1
            self._rows[dossier.dossier_id] = (dossier, version)
            return DossierWrite(
                dossier_id=dossier.dossier_id,
                version=version,
                written=True,
                content_digest=dossier.content_digest,
            )

    def get(self, dossier_id: str) -> IncidentDossier | None:
        with self._lock:
            row = self._rows.get(dossier_id)
            return row[0] if row is not None else None

    def list(self, *, status: DossierStatus | None = None) -> list[IncidentDossier]:
        with self._lock:
            dossiers = [row[0] for row in self._rows.values()]
        if status is not None:
            dossiers = [item for item in dossiers if item.status == status]
        return sorted(dossiers, key=lambda item: (-item.updated_at.timestamp(), item.dossier_id))

    def search(
        self,
        *,
        asset_id: str | None = None,
        fault_family: str | None = None,
        symptom_fingerprint: str | None = None,
        limit: int = 50,
    ) -> list[IncidentDossier]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        asset_id = asset_id.strip() if asset_id is not None else None
        fault_family = fault_family.strip() if fault_family is not None else None
        symptom_fingerprint = (
            symptom_fingerprint.strip() if symptom_fingerprint is not None else None
        )
        if not any((asset_id, fault_family, symptom_fingerprint)):
            raise ValueError("search requires asset_id, fault_family, or symptom_fingerprint")
        matches = self.list()
        if asset_id:
            matches = [item for item in matches if asset_id in item.asset_ids]
        if fault_family:
            matches = [item for item in matches if item.fault_family == fault_family]
        if symptom_fingerprint:
            matches = [item for item in matches if item.symptom_fingerprint == symptom_fingerprint]
        return matches[:limit]


def _row_time(row: Mapping[str, Any]) -> datetime:
    raw = str(row.get("at") or "").strip()
    if not raw:
        raise ValueError("sentinel row requires at")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return _aware_utc(parsed)


def from_sentinel_chain(
    rows: Sequence[Mapping[str, Any]], *, source_mode: SourceMode = "live"
) -> IncidentDossier:
    """Build a dossier from one completed or reported Sentinel chain.

    The detector becomes the fault family and an unconfirmed hypothesis origin.
    Confirmation requires separate analysis or operator evidence in a later
    snapshot.
    """
    if not rows:
        raise ValueError("sentinel chain must not be empty")
    copied = [dict(row) for row in rows]
    detected = next((row for row in copied if row.get("kind") == "detected"), copied[0])
    detector = str(detected.get("detector") or detected.get("family") or "unknown_fault").strip()
    subject = str(detected.get("subject") or detected.get("target") or "unknown_asset").strip()
    action = str(detected.get("action") or "").strip()
    times = [_row_time(row) for row in copied]
    opened_at, updated_at = min(times), max(times)
    canonical_rows = json.dumps(copied, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    chain_digest = hashlib.sha256(canonical_rows.encode("utf-8")).hexdigest()
    evidence: list[EvidenceReference] = []
    for index, (row, observed_at) in enumerate(zip(copied, times), start=1):
        row_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        evidence.append(
            EvidenceReference(
                evidence_id=f"sentinel:{chain_digest[:16]}:{index:04d}",
                source_type="replay_fixture" if source_mode == "replay" else "telemetry",
                locator=f"sentinel-chain:{chain_digest}:row:{index}",
                observed_at=observed_at,
                summary=str(row.get("summary") or row.get("reason") or row.get("note") or row.get("kind")),
                content_sha256=hashlib.sha256(row_json.encode("utf-8")).hexdigest(),
            )
        )
    evidence_ids = tuple(item.evidence_id for item in evidence)
    terminal = copied[-1]
    terminal_kind = str(terminal.get("kind") or "")
    passed = terminal_kind == "resolved" and str(terminal.get("outcome") or "passed") == "passed"

    attempts: tuple[RemediationAttempt, ...] = ()
    status: DossierStatus = "open"
    closed_at: datetime | None = None
    if action and terminal_kind != "no_safe_action":
        if passed:
            outcome = "succeeded"
        elif terminal_kind == "declined":
            outcome = "declined"
        elif terminal_kind == "escalated":
            outcome = "failed"
        else:
            outcome = "failed"
        receipt = ActionReceipt(
            receipt_id=f"receipt:{chain_digest[:20]}",
            executor="sentinel",
            started_at=opened_at,
            completed_at=updated_at,
            outcome=outcome,
            evidence_ids=evidence_ids,
        )
        readbacks: tuple[ReadbackResult, ...] = ()
        observation: ObservationWindow | None = None
        if passed:
            terminal_evidence = evidence_ids[-1]
            readbacks = (
                ReadbackResult(
                    readback_id=f"readback:{chain_digest[:20]}",
                    collected_at=updated_at,
                    verdict="passed",
                    checks=(
                        ReadbackCheck(
                            name="sentinel_resolution_verdict",
                            passed=True,
                            observed_value=str(terminal.get("outcome") or "passed"),
                            evidence_id=terminal_evidence,
                        ),
                    ),
                ),
            )
            opened_row = next((row for row in copied if row.get("kind") == "bakein_opened"), None)
            observation_start = _row_time(opened_row) if opened_row else opened_at
            hold = (updated_at - observation_start).total_seconds()
            planned = observation_start
            if opened_row and isinstance(opened_row.get("window_seconds"), (int, float)):
                planned = observation_start + timedelta(seconds=float(opened_row["window_seconds"]))
            observation = ObservationWindow(
                started_at=observation_start,
                planned_end_at=planned,
                verdict="passed",
                completed_at=updated_at,
                hold_duration_seconds=hold,
                evidence_ids=evidence_ids,
            )
            status = "resolved"
            closed_at = updated_at
        elif terminal_kind == "escalated":
            status = "escalated"
        attempts = (
            RemediationAttempt(
                attempt_id=f"attempt:{chain_digest[:20]}",
                action=action,
                target_asset_id=subject,
                initiated_at=opened_at,
                outcome=outcome,
                precondition_evidence_ids=evidence_ids,
                receipt=receipt,
                readbacks=readbacks,
                observation=observation,
            ),
        )
    elif terminal_kind == "escalated":
        status = "escalated"

    fingerprint = hashlib.sha256(f"{detector}\0{subject}".encode("utf-8")).hexdigest()
    root_candidate = RootCauseHypothesis(
        hypothesis_id=f"hypothesis:{chain_digest[:20]}",
        statement=f"Observed {detector} symptoms on {subject}; causal mechanism requires confirmation.",
        status="hypothesis",
        origin="detector",
        detector_id=detector,
        confidence=0.0,
        evidence_ids=(evidence_ids[0],),
        updated_at=updated_at,
    )
    return IncidentDossier(
        dossier_id=f"dossier:{chain_digest}",
        source_mode=source_mode,
        status=status,
        fault_family=detector,
        fault_summary=str(detected.get("summary") or f"{detector} observed on {subject}"),
        severity=str(detected.get("severity") or "medium"),
        symptom_fingerprint=fingerprint,
        asset_ids=(subject,),
        opened_at=opened_at,
        updated_at=updated_at,
        closed_at=closed_at,
        evidence=tuple(evidence),
        root_causes=(root_candidate,),
        remediation_attempts=attempts,
    )


def from_risk_pattern(pattern: Any) -> IncidentDossier:
    """Open an investigation dossier from a verified, real risk aggregate.

    The pattern proves repeated observations and defines their evidence range.
    It does not prove a causal mechanism, so the generated root candidate stays
    detector-origin and unconfirmed until analysis or an operator disposition
    adds independent evidence.
    """
    from domains.network_rca.risk_pattern import RiskPattern

    if not isinstance(pattern, RiskPattern):
        raise TypeError("pattern must be a RiskPattern")
    if pattern.provenance != "real":
        raise ValueError("only real risk patterns may open production dossiers")
    snapshot = pattern.to_dict()
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    snapshot_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(pattern.pattern_id.encode("utf-8")).hexdigest()
    assets = pattern.target_assets or (pattern.scope_key,)
    family = f"security_{pattern.risk_type}"
    fingerprint = hashlib.sha256(
        f"{family}\0{pattern.scope_key}\0{pattern.target_service or ''}".encode("utf-8")
    ).hexdigest()
    severity = (
        "critical"
        if pattern.risk_type in {"credential_attack", "admin_login_failed", "admin_login_lockout"}
        and pattern.event_count >= 100
        else "high"
    )
    evidence_id = f"risk-pattern:{snapshot_digest}"
    locator = "risk-pattern:" + json.dumps(
        {
            "pattern_id": pattern.pattern_id,
            "query_ranges": [item.to_dict() for item in pattern.evidence_query_ranges],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence = EvidenceReference(
        evidence_id=evidence_id,
        source_type="telemetry",
        locator=locator,
        observed_at=pattern.last_seen,
        summary=(
            f"{pattern.event_count} {pattern.risk_type} observations across "
            f"{pattern.active_days} active days"
        ),
        content_sha256=snapshot_digest,
    )
    hypothesis = RootCauseHypothesis(
        hypothesis_id=f"hypothesis:risk:{identity[:20]}",
        statement=(
            f"Repeated {pattern.risk_type} observations in {pattern.scope_key}; "
            "attack path and exposed control require causal confirmation."
        ),
        status="hypothesis",
        origin="detector",
        detector_id=f"risk-pattern:{pattern.risk_type}",
        confidence=0.0,
        evidence_ids=(evidence_id,),
        updated_at=pattern.last_seen,
    )
    return IncidentDossier(
        dossier_id=f"dossier:risk:{identity}",
        source_mode="live",
        status="open",
        fault_family=family,
        fault_summary=(
            f"{pattern.risk_type} campaign on {pattern.scope_key} "
            f"({pattern.event_count} observations)"
        ),
        severity=severity,
        symptom_fingerprint=fingerprint,
        asset_ids=assets,
        opened_at=pattern.first_seen,
        updated_at=pattern.last_seen,
        evidence=(evidence,),
        root_causes=(hypothesis,),
    )
