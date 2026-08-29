"""Auditable promotion of verified network findings into reusable features.

The feature layer is deliberately downstream of incident and risk records.  It
does not diagnose an incident and it does not turn a successful action into a
root cause.  It answers the narrower, reproducible question: have enough
independent, verified cases supported the same scoped finding for investigators
to use it as a ranking prior?

The module has no storage or model dependency.  ``FeatureStore`` can be dumped
and loaded deterministically, while ``NetworkFeatureEngine`` performs the pure
projection and records every promotion, retention, demotion, and revocation
decision.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence


FeatureState = Literal["candidate", "promoted", "revoked"]
FeatureSignal = Literal["fault_pattern", "remediation_effect", "risk_pattern"]
EvidenceVerdict = Literal["support", "counterexample"]
_RISK_AGGREGATION_WINDOW_SECONDS = 90 * 24 * 60 * 60


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field_name=field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return _utc(parsed, field_name=field_name)


def _tuple_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        values = value
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _tuple_in_order(value: Any) -> tuple[str, ...]:
    """Normalize an ordered identifier history without reordering it."""

    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        values = value
    output: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return tuple(output)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(prefix: str, value: Any) -> str:
    encoded = _canonical(value).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _plain(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    for method_name in ("to_dict", "as_dict"):
        method = getattr(source, method_name, None)
        if callable(method):
            value = method()
            if not isinstance(value, Mapping):
                raise TypeError(f"{method_name}() must return a mapping")
            return dict(value)
    model_dump = getattr(source, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
        if not isinstance(value, Mapping):
            raise TypeError("model_dump() must return a mapping")
        return dict(value)
    if is_dataclass(source):
        return asdict(source)
    raise TypeError("source must be a mapping, dataclass, or expose to_dict()/as_dict()")


def _first(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


@dataclass(frozen=True, slots=True)
class MetricWindow:
    """The measured quantity and observation window behind a feature."""

    metric: str
    aggregation: str
    duration_seconds: int

    def __post_init__(self) -> None:
        if not self.metric.strip() or not self.aggregation.strip():
            raise ValueError("metric and aggregation are required")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregation": self.aggregation,
            "duration_seconds": self.duration_seconds,
            "metric": self.metric,
        }

    @classmethod
    def from_value(cls, value: Any) -> "MetricWindow":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("metric_window is required")
        return cls(
            metric=str(_first(value, "metric", "name", "metric_name", default="")).strip(),
            aggregation=str(_first(value, "aggregation", "operation", default="count")).strip(),
            duration_seconds=int(
                _first(value, "duration_seconds", "window_seconds", "seconds", default=0)
            ),
        )


@dataclass(frozen=True, slots=True)
class FeatureScope:
    """Where a finding is applicable; empty asset/role tuples mean unscoped."""

    asset_ids: tuple[str, ...]
    roles: tuple[str, ...]
    fault_family: str
    config_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_ids", _tuple_strings(self.asset_ids))
        object.__setattr__(self, "roles", _tuple_strings(self.roles))
        if not self.fault_family.strip():
            raise ValueError("fault_family is required")
        if not self.config_version.strip():
            raise ValueError("config_version is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_ids": list(self.asset_ids),
            "config_version": self.config_version,
            "fault_family": self.fault_family,
            "roles": list(self.roles),
        }

    @classmethod
    def from_value(cls, value: Any) -> "FeatureScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("scope is required")
        return cls(
            asset_ids=_tuple_strings(_first(value, "asset_ids", "assets", default=())),
            roles=_tuple_strings(_first(value, "roles", "asset_roles", default=())),
            fault_family=str(_first(value, "fault_family", "incident_family", default="")).strip(),
            config_version=str(
                _first(value, "config_version", "network_config_version", default="")
            ).strip(),
        )


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """One verified, independently attributable feature signal."""

    observation_id: str
    source_type: Literal["incident_dossier", "risk_pattern"]
    source_id: str
    independence_key: str
    signal: FeatureSignal
    statement: str
    verdict: EvidenceVerdict
    scope: FeatureScope
    metric_window: MetricWindow
    observed_at: datetime
    evidence_refs: tuple[str, ...] = ()
    weight: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.source_type not in {"incident_dossier", "risk_pattern"}:
            raise ValueError(f"unsupported source_type: {self.source_type!r}")
        if self.signal not in {"fault_pattern", "remediation_effect", "risk_pattern"}:
            raise ValueError(f"unsupported signal: {self.signal!r}")
        if self.verdict not in {"support", "counterexample"}:
            raise ValueError(f"unsupported verdict: {self.verdict!r}")
        for name in ("observation_id", "source_id", "independence_key", "statement"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, field_name="observed_at"))
        object.__setattr__(self, "evidence_refs", _tuple_strings(self.evidence_refs))
        if not math.isfinite(self.weight) or not 0.0 < self.weight <= 1.0:
            raise ValueError("weight must be finite and in (0, 1]")
        if self.valid_from is not None:
            object.__setattr__(
                self, "valid_from", _utc(self.valid_from, field_name="valid_from")
            )
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", _utc(self.valid_to, field_name="valid_to"))
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")

    @property
    def feature_id(self) -> str:
        return _digest(
            "nf",
            {
                "metric_window": self.metric_window.to_dict(),
                "scope": self.scope.to_dict(),
                "signal": self.signal,
                "statement": self.statement,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_refs": list(self.evidence_refs),
            "independence_key": self.independence_key,
            "metric_window": self.metric_window.to_dict(),
            "observation_id": self.observation_id,
            "observed_at": _iso(self.observed_at),
            "scope": self.scope.to_dict(),
            "signal": self.signal,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "statement": self.statement,
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
            "verdict": self.verdict,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FeatureObservation":
        return cls(
            observation_id=str(row["observation_id"]),
            source_type=str(row["source_type"]),  # type: ignore[arg-type]
            source_id=str(row["source_id"]),
            independence_key=str(row["independence_key"]),
            signal=str(row["signal"]),  # type: ignore[arg-type]
            statement=str(row["statement"]),
            verdict=str(row["verdict"]),  # type: ignore[arg-type]
            scope=FeatureScope.from_value(row["scope"]),
            metric_window=MetricWindow.from_value(row["metric_window"]),
            observed_at=_parse_time(row["observed_at"], field_name="observed_at"),
            evidence_refs=_tuple_strings(row.get("evidence_refs")),
            weight=float(row.get("weight", 1.0)),
            valid_from=(
                _parse_time(row["valid_from"], field_name="valid_from")
                if row.get("valid_from")
                else None
            ),
            valid_to=(
                _parse_time(row["valid_to"], field_name="valid_to")
                if row.get("valid_to")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    min_independent_support: int = 3
    promotion_confidence: float = 0.70
    retention_confidence: float = 0.55
    min_effective_support: float = 1.50
    revoke_counterexamples: int = 2
    revoke_ratio: float = 0.40
    half_life_days: float = 90.0

    def __post_init__(self) -> None:
        if self.min_independent_support < 2:
            raise ValueError("promotion requires at least two independent cases")
        if self.revoke_counterexamples < 1:
            raise ValueError("revoke_counterexamples must be positive")
        for name in ("promotion_confidence", "retention_confidence", "revoke_ratio"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.retention_confidence > self.promotion_confidence:
            raise ValueError("retention_confidence cannot exceed promotion_confidence")
        if self.min_effective_support <= 0 or self.half_life_days <= 0:
            raise ValueError("effective support and half-life must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class NetworkFeature:
    feature_id: str
    signal: FeatureSignal
    statement: str
    scope: FeatureScope
    metric_window: MetricWindow
    state: FeatureState
    sample_size: int
    support_count: int
    counterexample_count: int
    supporting_case_ids: tuple[str, ...]
    counterexample_case_ids: tuple[str, ...]
    confidence: float
    effective_support: float
    effective_counterexamples: float
    last_verified: datetime
    valid_from: datetime
    valid_to: datetime | None
    updated_at: datetime
    decision_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "counterexample_case_ids": list(self.counterexample_case_ids),
            "counterexample_count": self.counterexample_count,
            "decision_ids": list(self.decision_ids),
            "effective_counterexamples": self.effective_counterexamples,
            "effective_support": self.effective_support,
            "feature_id": self.feature_id,
            "last_verified": _iso(self.last_verified),
            "metric_window": self.metric_window.to_dict(),
            "sample_size": self.sample_size,
            "scope": self.scope.to_dict(),
            "signal": self.signal,
            "state": self.state,
            "statement": self.statement,
            "support_count": self.support_count,
            "supporting_case_ids": list(self.supporting_case_ids),
            "updated_at": _iso(self.updated_at),
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "NetworkFeature":
        return cls(
            feature_id=str(row["feature_id"]),
            signal=str(row["signal"]),  # type: ignore[arg-type]
            statement=str(row["statement"]),
            scope=FeatureScope.from_value(row["scope"]),
            metric_window=MetricWindow.from_value(row["metric_window"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            sample_size=int(row["sample_size"]),
            support_count=int(row["support_count"]),
            counterexample_count=int(row["counterexample_count"]),
            supporting_case_ids=_tuple_strings(row.get("supporting_case_ids")),
            counterexample_case_ids=_tuple_strings(row.get("counterexample_case_ids")),
            confidence=float(row["confidence"]),
            effective_support=float(row["effective_support"]),
            effective_counterexamples=float(row["effective_counterexamples"]),
            last_verified=_parse_time(row["last_verified"], field_name="last_verified"),
            valid_from=_parse_time(row["valid_from"], field_name="valid_from"),
            valid_to=(
                _parse_time(row["valid_to"], field_name="valid_to")
                if row.get("valid_to")
                else None
            ),
            updated_at=_parse_time(row["updated_at"], field_name="updated_at"),
            decision_ids=_tuple_in_order(row.get("decision_ids")),
        )


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision_id: str
    feature_id: str
    decided_at: datetime
    previous_state: FeatureState | None
    resulting_state: FeatureState
    action: Literal[
        "created_candidate", "held_candidate", "promoted", "retained", "demoted", "revoked"
    ]
    reason_codes: tuple[str, ...]
    support_count: int
    counterexample_count: int
    effective_support: float
    effective_counterexamples: float
    confidence: float
    evidence_digest: str
    policy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "counterexample_count": self.counterexample_count,
            "decided_at": _iso(self.decided_at),
            "decision_id": self.decision_id,
            "effective_counterexamples": self.effective_counterexamples,
            "effective_support": self.effective_support,
            "evidence_digest": self.evidence_digest,
            "feature_id": self.feature_id,
            "policy": dict(sorted(self.policy.items())),
            "previous_state": self.previous_state,
            "reason_codes": list(self.reason_codes),
            "resulting_state": self.resulting_state,
            "support_count": self.support_count,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "PromotionDecision":
        return cls(
            decision_id=str(row["decision_id"]),
            feature_id=str(row["feature_id"]),
            decided_at=_parse_time(row["decided_at"], field_name="decided_at"),
            previous_state=row.get("previous_state"),  # type: ignore[arg-type]
            resulting_state=str(row["resulting_state"]),  # type: ignore[arg-type]
            action=str(row["action"]),  # type: ignore[arg-type]
            reason_codes=_tuple_strings(row.get("reason_codes")),
            support_count=int(row["support_count"]),
            counterexample_count=int(row["counterexample_count"]),
            effective_support=float(row["effective_support"]),
            effective_counterexamples=float(row["effective_counterexamples"]),
            confidence=float(row["confidence"]),
            evidence_digest=str(row["evidence_digest"]),
            policy=dict(row.get("policy", {})),
        )


@dataclass(frozen=True, slots=True)
class FeatureMatch:
    feature: NetworkFeature
    score: float
    matched_on: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature.to_dict(),
            "matched_on": list(self.matched_on),
            "score": self.score,
        }


class FeatureStore:
    """In-memory repository with deterministic snapshots and idempotent upserts."""

    def __init__(self) -> None:
        self._observations: dict[str, FeatureObservation] = {}
        self._observation_ids_by_feature: dict[str, set[str]] = {}
        self._features: dict[str, NetworkFeature] = {}
        self._decisions: dict[str, PromotionDecision] = {}
        self._decision_ids_by_feature: dict[str, set[str]] = {}

    def upsert_observation(self, observation: FeatureObservation) -> bool:
        previous = self._observations.get(observation.observation_id)
        if previous == observation:
            return False
        if previous is not None and previous.feature_id != observation.feature_id:
            old_ids = self._observation_ids_by_feature.get(previous.feature_id)
            if old_ids is not None:
                old_ids.discard(previous.observation_id)
                if not old_ids:
                    del self._observation_ids_by_feature[previous.feature_id]
        self._observations[observation.observation_id] = observation
        self._observation_ids_by_feature.setdefault(observation.feature_id, set()).add(
            observation.observation_id
        )
        return True

    def observations_for(self, feature_id: str) -> tuple[FeatureObservation, ...]:
        observation_ids = self._observation_ids_by_feature.get(feature_id, set())
        return tuple(
            sorted(
                (self._observations[row_id] for row_id in observation_ids),
                key=lambda row: (row.observed_at, row.observation_id),
            )
        )

    def feature_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._observation_ids_by_feature))

    def put_feature(self, feature: NetworkFeature) -> None:
        self._features[feature.feature_id] = feature

    def get(self, feature_id: str) -> NetworkFeature | None:
        return self._features.get(feature_id)

    def features(self) -> tuple[NetworkFeature, ...]:
        return tuple(self._features[key] for key in sorted(self._features))

    def put_decision(self, decision: PromotionDecision) -> bool:
        if decision.decision_id in self._decisions:
            return False
        self._decisions[decision.decision_id] = decision
        self._decision_ids_by_feature.setdefault(decision.feature_id, set()).add(
            decision.decision_id
        )
        return True

    def decisions_for(self, feature_id: str) -> tuple[PromotionDecision, ...]:
        return tuple(
            sorted(
                (
                    self._decisions[decision_id]
                    for decision_id in self._decision_ids_by_feature.get(feature_id, set())
                ),
                key=lambda row: (row.decided_at, row.decision_id),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": [self._decisions[key].to_dict() for key in sorted(self._decisions)],
            "features": [self._features[key].to_dict() for key in sorted(self._features)],
            "observations": [
                self._observations[key].to_dict() for key in sorted(self._observations)
            ],
            "schema_version": 1,
        }

    def dumps(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FeatureStore":
        if int(payload.get("schema_version", 0)) != 1:
            raise ValueError("unsupported FeatureStore schema version")
        store = cls()
        for row in payload.get("observations", []):
            store.upsert_observation(FeatureObservation.from_dict(row))
        for row in payload.get("features", []):
            store.put_feature(NetworkFeature.from_dict(row))
        for row in payload.get("decisions", []):
            store.put_decision(PromotionDecision.from_dict(row))
        return store

    @classmethod
    def loads(cls, payload: str) -> "FeatureStore":
        value = json.loads(payload)
        if not isinstance(value, Mapping):
            raise ValueError("FeatureStore snapshot must be a JSON object")
        return cls.from_dict(value)


class NetworkFeatureEngine:
    """Consolidate verified observations and expose promoted investigation priors."""

    def __init__(
        self,
        store: FeatureStore | None = None,
        *,
        policy: PromotionPolicy | None = None,
    ) -> None:
        self.store = store or FeatureStore()
        self.policy = policy or PromotionPolicy()

    def observe(self, observation: FeatureObservation, *, now: datetime) -> NetworkFeature:
        now = _utc(now, field_name="now")
        changed = self.store.upsert_observation(observation)
        current = self.store.get(observation.feature_id)
        if not changed and current is not None:
            return current
        return self.reassess(observation.feature_id, now=now)

    def update(self, source: Any, *, now: datetime) -> tuple[NetworkFeature, ...]:
        """Update from an IncidentDossier, RiskPattern, or their mapping form."""

        observations = observations_from_source(source)
        updated: dict[str, NetworkFeature] = {}
        for observation in observations:
            updated[observation.feature_id] = self.observe(observation, now=now)
        return tuple(updated[key] for key in sorted(updated))

    def reassess_all(self, *, now: datetime) -> tuple[NetworkFeature, ...]:
        return tuple(self.reassess(feature_id, now=now) for feature_id in self.store.feature_ids())

    def reassess(self, feature_id: str, *, now: datetime) -> NetworkFeature:
        now = _utc(now, field_name="now")
        observations = self.store.observations_for(feature_id)
        if not observations:
            raise KeyError(feature_id)
        previous = self.store.get(feature_id)

        # One independence key contributes at most once.  A later verified
        # revision supersedes an earlier one and cannot be used on both sides.
        latest_by_key: dict[str, FeatureObservation] = {}
        for row in observations:
            candidate = latest_by_key.get(row.independence_key)
            if candidate is None or (row.observed_at, row.observation_id) > (
                candidate.observed_at,
                candidate.observation_id,
            ):
                latest_by_key[row.independence_key] = row
        independent = tuple(
            sorted(latest_by_key.values(), key=lambda row: (row.observed_at, row.observation_id))
        )

        def effective(row: FeatureObservation) -> float:
            age_days = max(0.0, (now - row.observed_at).total_seconds() / 86400.0)
            return row.weight * (0.5 ** (age_days / self.policy.half_life_days))

        supports = tuple(row for row in independent if row.verdict == "support")
        counters = tuple(row for row in independent if row.verdict == "counterexample")
        support_weight = sum(effective(row) for row in supports)
        counter_weight = sum(effective(row) for row in counters)
        # A one-unit uncertainty prior prevents a single success from looking
        # certain.  Three full-weight independent supports yield 0.75.
        confidence = support_weight / (support_weight + counter_weight + 1.0)
        raw_total = len(supports) + len(counters)
        counter_ratio = len(counters) / raw_total if raw_total else 0.0
        strongly_refuted = (
            len(counters) >= self.policy.revoke_counterexamples
            and counter_ratio >= self.policy.revoke_ratio
        )
        promotable = (
            len(supports) >= self.policy.min_independent_support
            and support_weight >= self.policy.min_effective_support
            and confidence >= self.policy.promotion_confidence
            and not strongly_refuted
        )

        previous_state = previous.state if previous else None
        reasons: list[str] = []
        if len(supports) < self.policy.min_independent_support:
            reasons.append("insufficient_independent_support")
        if support_weight < self.policy.min_effective_support:
            reasons.append("insufficient_effective_support")
        if confidence < self.policy.promotion_confidence:
            reasons.append("below_promotion_confidence")
        if strongly_refuted:
            reasons.append("counterexample_revocation_threshold")

        if previous_state == "promoted":
            if strongly_refuted:
                state: FeatureState = "revoked"
                action = "revoked"
            elif (
                confidence < self.policy.retention_confidence
                or support_weight < self.policy.min_effective_support
            ):
                state = "candidate"
                action = "demoted"
                if confidence < self.policy.retention_confidence:
                    reasons.append("below_retention_confidence")
                if support_weight < self.policy.min_effective_support:
                    reasons.append("support_decayed_below_retention")
            else:
                state = "promoted"
                action = "retained"
                reasons = ["retention_thresholds_met"]
        elif promotable:
            state = "promoted"
            action = "promoted"
            reasons = ["promotion_thresholds_met"]
        else:
            # Counterexamples stop a candidate from advancing.  Revocation is a
            # lifecycle transition for a feature that was previously trusted.
            state = "candidate"
            action = "created_candidate" if previous is None else "held_candidate"

        evidence_payload = [row.to_dict() for row in independent]
        evidence_digest = _digest("evidence", evidence_payload)
        decision_payload = {
            # Time-decay checks run on reads.  A daily decision bucket keeps
            # that audit useful without creating one new decision for every UI
            # refresh while the evidence cut and resulting state are unchanged.
            "day": now.date().isoformat(),
            "evidence_digest": evidence_digest,
            "feature_id": feature_id,
            "previous_state": previous_state,
            "resulting_state": state,
        }
        decision_id = _digest("decision", decision_payload)
        decision = PromotionDecision(
            decision_id=decision_id,
            feature_id=feature_id,
            decided_at=now,
            previous_state=previous_state,
            resulting_state=state,
            action=action,  # type: ignore[arg-type]
            reason_codes=tuple(sorted(set(reasons))),
            support_count=len(supports),
            counterexample_count=len(counters),
            effective_support=float(round(support_weight, 12)),
            effective_counterexamples=float(round(counter_weight, 12)),
            confidence=round(confidence, 12),
            evidence_digest=evidence_digest,
            policy=self.policy.to_dict(),
        )
        is_new_decision = self.store.put_decision(decision)

        first_valid = min((row.valid_from or row.observed_at) for row in independent)
        last_verified = max(row.observed_at for row in independent)
        valid_to = previous.valid_to if previous else None
        if state == "promoted":
            valid_to = None
        elif previous_state == "promoted" and state != "promoted":
            valid_to = now

        decision_ids = previous.decision_ids if previous else ()
        if is_new_decision:
            decision_ids = (*decision_ids, decision_id)
        seed = independent[0]
        feature = NetworkFeature(
            feature_id=feature_id,
            signal=seed.signal,
            statement=seed.statement,
            scope=seed.scope,
            metric_window=seed.metric_window,
            state=state,
            sample_size=len(independent),
            support_count=len(supports),
            counterexample_count=len(counters),
            supporting_case_ids=_tuple_strings(row.source_id for row in supports),
            counterexample_case_ids=_tuple_strings(row.source_id for row in counters),
            confidence=round(confidence, 12),
            effective_support=float(round(support_weight, 12)),
            effective_counterexamples=float(round(counter_weight, 12)),
            last_verified=last_verified,
            valid_from=first_valid,
            valid_to=valid_to,
            updated_at=now,
            decision_ids=decision_ids,
        )
        self.store.put_feature(feature)
        return feature

    def rank_for_investigation(
        self,
        *,
        at: datetime,
        asset_ids: Sequence[str] = (),
        roles: Sequence[str] = (),
        fault_family: str | None = None,
        config_version: str | None = None,
        limit: int = 10,
    ) -> tuple[FeatureMatch, ...]:
        """Return current promoted features compatible with investigation scope.

        Calling this method also applies and audits time decay at ``at``.  That
        keeps a stale feature from influencing ranking merely because no writer
        happened to touch it recently.
        """

        if limit <= 0:
            return ()
        at = _utc(at, field_name="at")
        wanted_assets = set(_tuple_strings(asset_ids))
        wanted_roles = set(_tuple_strings(roles))
        # Scope is stable across reassessment, so first narrow the feature ids
        # to records that could influence this investigation.  Reassessing all
        # network-wide features on every single-host lookup made the first
        # evidence receipt wait on thousands of unrelated promotion decisions.
        candidate_ids: list[str] = []
        for feature in self.store.features():
            scope = feature.scope
            if scope.asset_ids and wanted_assets and not wanted_assets.intersection(scope.asset_ids):
                continue
            if scope.roles and wanted_roles and not wanted_roles.intersection(scope.roles):
                continue
            if fault_family and scope.fault_family != fault_family:
                continue
            if (
                config_version
                and scope.config_version not in {config_version, "unversioned"}
            ):
                continue
            candidate_ids.append(feature.feature_id)
        for feature_id in candidate_ids:
            self.reassess(feature_id, now=at)

        matches: list[FeatureMatch] = []
        for feature_id in candidate_ids:
            feature = self.store.get(feature_id)
            if feature is None:
                continue
            if feature.state != "promoted":
                continue
            scope = feature.scope
            matched: list[str] = []
            specificity = 0.0
            if wanted_assets.intersection(scope.asset_ids):
                matched.append("asset")
                specificity += 0.20
            if wanted_roles.intersection(scope.roles):
                matched.append("role")
                specificity += 0.10
            if fault_family and scope.fault_family == fault_family:
                matched.append("fault_family")
                specificity += 0.15
            if config_version and scope.config_version == config_version:
                matched.append("config_version")
                specificity += 0.05
            matches.append(
                FeatureMatch(
                    feature=feature,
                    score=round(feature.confidence + specificity, 12),
                    matched_on=tuple(matched),
                )
            )
        matches.sort(key=lambda row: (-row.score, row.feature.feature_id))
        return tuple(matches[:limit])


def _scope_and_metric(row: Mapping[str, Any]) -> tuple[FeatureScope, MetricWindow]:
    scope_value = row.get("scope")
    if scope_value is None:
        scope_value = {
            "asset_ids": _first(row, "asset_ids", "assets", "affected_assets", default=()),
            "roles": _first(row, "roles", "asset_roles", default=()),
            "fault_family": _first(row, "fault_family", "incident_family", default=""),
            "config_version": _first(
                row, "config_version", "network_config_version", default=""
            ),
        }
    metric_value = _first(row, "metric_window", "metric", default=None)
    return FeatureScope.from_value(scope_value), MetricWindow.from_value(metric_value)


def _observation(
    *,
    row: Mapping[str, Any],
    source_type: Literal["incident_dossier", "risk_pattern"],
    source_id: str,
    signal: FeatureSignal,
    statement: str,
    verdict: EvidenceVerdict,
    suffix: str,
    weight: float,
    observed_at: datetime,
    evidence_refs: Any,
    scope: FeatureScope,
    metric_window: MetricWindow,
) -> FeatureObservation:
    identity = {"source_type": source_type, "source_id": source_id, "suffix": suffix}
    return FeatureObservation(
        observation_id=_digest("obs", identity),
        source_type=source_type,
        source_id=source_id,
        independence_key=str(
            _first(row, "independence_key", "incident_group_id", "campaign_id", default=source_id)
        ),
        signal=signal,
        statement=statement,
        verdict=verdict,
        scope=scope,
        metric_window=metric_window,
        observed_at=observed_at,
        evidence_refs=_tuple_strings(evidence_refs),
        weight=weight,
        valid_from=(
            _parse_time(row["valid_from"], field_name="valid_from")
            if row.get("valid_from")
            else observed_at
        ),
        valid_to=(
            _parse_time(row["valid_to"], field_name="valid_to")
            if row.get("valid_to")
            else None
        ),
    )


def observations_from_incident_dossier(source: Any) -> tuple[FeatureObservation, ...]:
    """Extract root-cause and action-effect signals from a verified dossier.

    Root-cause evidence requires an explicit confirmed/refuted root-cause
    status.  Remediation results are extracted separately, even when an action
    succeeded, so action success can never confirm the root cause by accident.
    """

    row = _plain(source)
    source_id = str(_first(row, "dossier_id", "incident_id", "id", default="")).strip()
    if not source_id:
        raise ValueError("incident dossier id is required")
    is_native_dossier = "root_causes" in row and "asset_ids" in row
    if is_native_dossier:
        # Native dossiers separate causal confirmation from action effect.  A
        # replay or drill remains available for audit but cannot train the
        # production feature store.
        source_mode = str(row.get("source_mode") or "").lower()
        if source_mode != "live":
            return ()
        opened_at = _parse_time(row.get("opened_at"), field_name="opened_at")
        updated_at = _parse_time(row.get("updated_at"), field_name="updated_at")
        row.update({
            "verified": True,
            "observed_at": row.get("updated_at"),
            "scope": {
                "asset_ids": row.get("asset_ids") or (),
                "roles": (),
                "fault_family": row.get("fault_family"),
                "config_version": "unversioned",
            },
            "metric_window": {
                "metric": "incident_outcome",
                "aggregation": "last",
                "duration_seconds": max(1, int((updated_at - opened_at).total_seconds())),
            },
            "root_cause": None,
            "remediations": (),
            "evidence_refs": [
                evidence.get("evidence_id")
                for evidence in row.get("evidence") or ()
                if isinstance(evidence, Mapping) and evidence.get("evidence_id")
            ],
        })
    verified = bool(_first(row, "verified", "is_verified", default=False))
    if not verified:
        return ()
    scope, metric_window = _scope_and_metric(row)
    observed_at = _parse_time(
        _first(row, "observed_at", "closed_at", "updated_at"), field_name="observed_at"
    )
    common_refs = _first(row, "evidence_refs", "evidence_ids", default=())
    output: list[FeatureObservation] = []

    root_values: Any = row.get("root_causes") if is_native_dossier else row.get("root_cause")
    if isinstance(root_values, Mapping):
        root_values = (root_values,)
    for root_value in root_values or ():
        if not isinstance(root_value, Mapping):
            continue
        root = dict(root_value)
        root_key = str(
            _first(root, "key", "root_cause_key", "name", "statement", default="")
        ).strip()
        root_status = str(_first(root, "status", "verification_status", default="")).lower()
        if root_key and root_status in {"confirmed", "refuted"}:
            output.append(
                _observation(
                    row=row,
                    source_type="incident_dossier",
                    source_id=source_id,
                    signal="fault_pattern",
                    statement=f"root_cause:{root_key}",
                    verdict="support" if root_status == "confirmed" else "counterexample",
                    suffix=f"root:{root_key}",
                    weight=max(0.000001, float(root.get("confidence", 1.0))),
                    observed_at=observed_at,
                    evidence_refs=_first(root, "evidence_refs", default=common_refs),
                    scope=scope,
                    metric_window=metric_window,
                )
            )

    remediation_values = (
        row.get("remediation_attempts")
        if is_native_dossier
        else _first(row, "remediations", "actions", default=())
    )
    if isinstance(remediation_values, Mapping):
        remediation_values = (remediation_values,)
    for index, raw_action in enumerate(remediation_values or ()):
        if not isinstance(raw_action, Mapping):
            continue
        action = dict(raw_action)
        if is_native_dossier:
            observation = action.get("observation")
            readbacks = action.get("readbacks") or ()
            observation_verdict = (
                str(observation.get("verdict") or "").lower()
                if isinstance(observation, Mapping) else ""
            )
            readback_verified = any(
                isinstance(item, Mapping) and item.get("verdict") == "passed"
                for item in readbacks
            )
            raw_outcome = str(action.get("outcome") or "").lower()
            if raw_outcome == "succeeded" and observation_verdict == "passed":
                normalized_outcome = "effective"
            elif raw_outcome in {"failed", "rolled_back"} or observation_verdict == "failed":
                normalized_outcome = "ineffective"
            else:
                normalized_outcome = raw_outcome
            receipt = action.get("receipt")
            action.update({
                "verified": readback_verified and observation_verdict in {"passed", "failed"},
                "outcome": normalized_outcome,
                "verified_at": (
                    observation.get("completed_at")
                    if isinstance(observation, Mapping) and observation.get("completed_at")
                    else receipt.get("completed_at")
                    if isinstance(receipt, Mapping) else row.get("updated_at")
                ),
            })
        action_name = str(_first(action, "action", "name", "action_key", default="")).strip()
        action_verified = bool(
            _first(action, "verified", "readback_verified", "effect_verified", default=False)
        )
        outcome = str(_first(action, "outcome", "status", default="")).lower()
        if not action_name or not action_verified:
            continue
        if outcome in {"passed", "success", "successful", "effective", "held"}:
            verdict: EvidenceVerdict = "support"
        elif outcome in {"failed", "failure", "ineffective", "regressed", "reverted"}:
            verdict = "counterexample"
        else:
            continue
        action_metric = metric_window
        if _first(action, "metric_window", "metric", default=None) is not None:
            action_metric = MetricWindow.from_value(
                _first(action, "metric_window", "metric", default=None)
            )
        output.append(
            _observation(
                row=row,
                source_type="incident_dossier",
                source_id=source_id,
                signal="remediation_effect",
                statement=f"action_effect:{action_name}",
                verdict=verdict,
                suffix=f"action:{index}:{action_name}",
                weight=float(action.get("confidence", 1.0)),
                observed_at=_parse_time(
                    _first(action, "verified_at", "observed_at", default=observed_at),
                    field_name="action.verified_at",
                ),
                evidence_refs=_first(action, "evidence_refs", default=common_refs),
                scope=scope,
                metric_window=action_metric,
            )
        )
    return tuple(sorted(output, key=lambda item: item.observation_id))


def observations_from_risk_pattern(source: Any) -> tuple[FeatureObservation, ...]:
    row = _plain(source)
    source_id = str(_first(row, "risk_id", "pattern_id", "id", default="")).strip()
    if not source_id:
        raise ValueError("risk pattern id is required")

    # ``risk_pattern.RiskPattern`` is itself a deterministic aggregate over
    # normalized facts.  Its public dictionary intentionally has no synthetic
    # ``verified`` flag, config version, or metric wrapper.  Adapt that native
    # shape explicitly and keep replay/drill aggregates out of production
    # feature evidence.  ``unversioned`` is retained as an honest scope value;
    # retrieval may use it as a broad prior but never awards a config match.
    is_native_aggregate = (
        "pattern_id" in row and "risk_type" in row and "event_count" in row
    )
    if is_native_aggregate:
        provenance = str(row.get("provenance") or "").lower()
        verified = provenance == "real" and int(row.get("event_count") or 0) > 0
        _parse_time(row.get("first_seen"), field_name="first_seen")
        _parse_time(row.get("last_seen"), field_name="last_seen")
        refs: list[str] = []
        for query_range in row.get("evidence_query_ranges") or ():
            if isinstance(query_range, Mapping):
                refs.extend(str(item) for item in query_range.get("sample_refs") or ())
        row.update(
            {
                "verified": verified,
                "observed_at": row.get("last_seen"),
                "assets": row.get("target_assets") or (),
                "roles": (),
                "fault_family": row.get("risk_type"),
                "config_version": "unversioned",
                "metric_window": {
                    "metric": "risk_event_count",
                    "aggregation": "count",
                    # The source query is a fixed 90-day window. Using the
                    # changing first-to-last duration in the feature identity
                    # created a new feature for every refresh of one pattern.
                    "duration_seconds": _RISK_AGGREGATION_WINDOW_SECONDS,
                },
                "pattern_type": row.get("risk_type"),
                "evidence_refs": refs,
                "valid_from": row.get("first_seen"),
                "valid_to": row.get("mitigated_at"),
            }
        )
    if not bool(_first(row, "verified", "is_verified", default=False)):
        return ()
    scope, metric_window = _scope_and_metric(row)
    pattern = str(_first(row, "pattern_type", "risk_type", "name", default="")).strip()
    if not pattern:
        raise ValueError("risk pattern type is required")
    status = str(_first(row, "verdict", "status", default="support")).lower()
    if status in {
        "support",
        "active",
        "confirmed",
        "verified",
        "recurring",
        "recurrent",
        "mitigated",
    }:
        verdict: EvidenceVerdict = "support"
    elif status in {"counterexample", "false_positive", "refuted", "invalid"}:
        verdict = "counterexample"
    else:
        return ()
    observed_at = _parse_time(
        _first(row, "observed_at", "last_seen", "updated_at"), field_name="observed_at"
    )
    return (
        _observation(
            row=row,
            source_type="risk_pattern",
            source_id=source_id,
            signal="risk_pattern",
            statement=f"risk:{pattern}",
            verdict=verdict,
            suffix=f"risk:{pattern}",
            weight=float(row.get("confidence", 1.0)),
            observed_at=observed_at,
            evidence_refs=_first(row, "evidence_refs", "evidence_ids", default=()),
            scope=scope,
            metric_window=metric_window,
        ),
    )


def observations_from_source(source: Any) -> tuple[FeatureObservation, ...]:
    if isinstance(source, FeatureObservation):
        return (source,)
    row = _plain(source)
    source_type = str(row.get("source_type") or row.get("record_type") or "").lower()
    class_name = source.__class__.__name__.lower()
    if source_type in {"incident", "incident_dossier", "incidentdossier"} or (
        not source_type and ("incident" in class_name or "dossier" in class_name)
    ):
        return observations_from_incident_dossier(row)
    if source_type in {"risk", "risk_pattern", "riskpattern"} or (
        not source_type and "risk" in class_name
    ):
        return observations_from_risk_pattern(row)
    # Mapping callers need not add a transport-only discriminator when their
    # business fields are already unambiguous.
    if "root_cause" in row or "root_causes" in row or "remediations" in row or "remediation_attempts" in row:
        return observations_from_incident_dossier(row)
    if "pattern_type" in row or "risk_type" in row:
        return observations_from_risk_pattern(row)
    raise ValueError("cannot determine whether source is an IncidentDossier or RiskPattern")


__all__ = [
    "FeatureMatch",
    "FeatureObservation",
    "FeatureScope",
    "FeatureStore",
    "MetricWindow",
    "NetworkFeature",
    "NetworkFeatureEngine",
    "PromotionDecision",
    "PromotionPolicy",
    "observations_from_incident_dossier",
    "observations_from_risk_pattern",
    "observations_from_source",
]
