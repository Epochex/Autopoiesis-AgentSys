from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from domains.network_rca.network_feature import (
    FeatureObservation,
    FeatureScope,
    FeatureStore,
    MetricWindow,
    NetworkFeatureEngine,
    PromotionPolicy,
    observations_from_incident_dossier,
    observations_from_risk_pattern,
)
from domains.network_rca.risk_pattern import RiskEvent, RiskPatternStore


UTC = timezone.utc
T0 = datetime(2026, 8, 1, 12, tzinfo=UTC)
SCOPE = FeatureScope(
    asset_ids=("switch-17",),
    roles=("access-switch",),
    fault_family="carrier_loss",
    config_version="netcfg-42",
)
WINDOW = MetricWindow(metric="carrier_transitions", aggregation="count", duration_seconds=900)


def observation(
    case: str,
    *,
    verdict: str = "support",
    at: datetime = T0,
    independence_key: str | None = None,
    signal: str = "fault_pattern",
    statement: str = "root_cause:damaged_uplink",
) -> FeatureObservation:
    return FeatureObservation(
        observation_id=f"obs-{case}",
        source_type="incident_dossier",
        source_id=case,
        independence_key=independence_key or case,
        signal=signal,  # type: ignore[arg-type]
        statement=statement,
        verdict=verdict,  # type: ignore[arg-type]
        scope=SCOPE,
        metric_window=WINDOW,
        observed_at=at,
        evidence_refs=(f"clickhouse:{case}",),
        valid_from=at - timedelta(minutes=15),
    )


def test_single_case_stays_candidate_and_duplicate_is_idempotent():
    engine = NetworkFeatureEngine()
    first = engine.observe(observation("inc-1"), now=T0)
    duplicate = engine.observe(observation("inc-1"), now=T0 + timedelta(days=1))

    assert first.state == "candidate"
    assert first.sample_size == first.support_count == 1
    assert first.confidence == 0.5
    assert duplicate == first
    assert len(engine.store.decisions_for(first.feature_id)) == 1
    assert engine.store.decisions_for(first.feature_id)[0].action == "created_candidate"


def test_independent_cases_promote_and_shared_incident_group_cannot_game_threshold():
    engine = NetworkFeatureEngine()
    engine.observe(observation("inc-1", independence_key="outage-a"), now=T0)
    same_outage = engine.observe(
        observation("inc-2", independence_key="outage-a", at=T0 + timedelta(minutes=1)),
        now=T0 + timedelta(minutes=1),
    )
    assert same_outage.support_count == 1

    engine.observe(
        observation("inc-3", independence_key="outage-b", at=T0 + timedelta(minutes=2)),
        now=T0 + timedelta(minutes=2),
    )
    promoted = engine.observe(
        observation("inc-4", independence_key="outage-c", at=T0 + timedelta(minutes=3)),
        now=T0 + timedelta(minutes=3),
    )

    assert promoted.state == "promoted"
    assert promoted.support_count == 3
    assert promoted.sample_size == 3
    assert promoted.confidence == pytest.approx(0.75, abs=2e-6)
    decision = engine.store.decisions_for(promoted.feature_id)[-1]
    assert decision.action == "promoted"
    assert decision.reason_codes == ("promotion_thresholds_met",)
    assert decision.evidence_digest.startswith("evidence-")
    assert decision.policy["min_independent_support"] == 3


def test_counterexamples_degrade_confidence_then_revoke_promoted_feature():
    engine = NetworkFeatureEngine()
    feature = None
    for index in range(3):
        feature = engine.observe(
            observation(f"support-{index}", at=T0 + timedelta(minutes=index)),
            now=T0 + timedelta(minutes=index),
        )
    assert feature is not None and feature.state == "promoted"

    one_failure = engine.observe(
        observation("failure-1", verdict="counterexample", at=T0 + timedelta(hours=1)),
        now=T0 + timedelta(hours=1),
    )
    assert one_failure.state == "promoted"
    assert one_failure.confidence < feature.confidence

    revoked = engine.observe(
        observation("failure-2", verdict="counterexample", at=T0 + timedelta(hours=2)),
        now=T0 + timedelta(hours=2),
    )
    assert revoked.state == "revoked"
    assert revoked.valid_to == T0 + timedelta(hours=2)
    assert revoked.counterexample_case_ids == ("failure-1", "failure-2")
    decision = engine.store.decisions_for(revoked.feature_id)[-1]
    assert decision.action == "revoked"
    assert "counterexample_revocation_threshold" in decision.reason_codes


def test_time_decay_is_explicit_and_can_demote_without_new_case():
    engine = NetworkFeatureEngine(policy=PromotionPolicy(half_life_days=30))
    feature = None
    for index in range(3):
        feature = engine.observe(observation(f"inc-{index}"), now=T0)
    assert feature is not None and feature.state == "promoted"

    stale = engine.reassess(feature.feature_id, now=T0 + timedelta(days=60))
    assert stale.state == "candidate"
    assert stale.effective_support == pytest.approx(0.75)
    assert stale.valid_to == T0 + timedelta(days=60)
    decision = engine.store.decisions_for(feature.feature_id)[-1]
    assert decision.action == "demoted"
    assert "support_decayed_below_retention" in decision.reason_codes


def _dossier(
    dossier_id: str,
    *,
    root_status: str,
    action_outcome: str = "passed",
) -> dict[str, object]:
    return {
        "source_type": "incident_dossier",
        "dossier_id": dossier_id,
        "verified": True,
        "observed_at": T0.isoformat(),
        "assets": ["switch-17"],
        "roles": ["access-switch"],
        "fault_family": "carrier_loss",
        "config_version": "netcfg-42",
        "metric_window": WINDOW.to_dict(),
        "root_cause": {
            "key": "damaged_uplink",
            "status": root_status,
            "confidence": 0.9,
            "evidence_refs": [f"root:{dossier_id}"],
        },
        "remediations": [
            {
                "action": "bounce_interface",
                "outcome": action_outcome,
                "verified": True,
                "verified_at": T0.isoformat(),
                "evidence_refs": [f"readback:{dossier_id}"],
            }
        ],
    }


def test_successful_action_is_only_effect_signal_and_never_confirms_pending_root_cause():
    observations = observations_from_incident_dossier(
        _dossier("inc-1", root_status="pending")
    )
    assert len(observations) == 1
    assert observations[0].signal == "remediation_effect"
    assert observations[0].statement == "action_effect:bounce_interface"
    assert observations[0].verdict == "support"


def test_explicitly_confirmed_root_and_verified_action_are_separate_features():
    observations = observations_from_incident_dossier(
        _dossier("inc-1", root_status="confirmed")
    )
    assert {(row.signal, row.statement) for row in observations} == {
        ("fault_pattern", "root_cause:damaged_uplink"),
        ("remediation_effect", "action_effect:bounce_interface"),
    }
    assert len({row.feature_id for row in observations}) == 2


def test_failed_action_is_counterexample_to_action_effect_not_root_cause():
    observations = observations_from_incident_dossier(
        _dossier("inc-1", root_status="pending", action_outcome="regressed")
    )
    assert [(row.signal, row.verdict) for row in observations] == [
        ("remediation_effect", "counterexample")
    ]


@dataclass(frozen=True)
class ExampleRiskPattern:
    source_type: str
    risk_id: str
    verified: bool
    observed_at: str
    assets: tuple[str, ...]
    roles: tuple[str, ...]
    fault_family: str
    config_version: str
    metric_window: dict[str, object]
    pattern_type: str
    status: str
    evidence_refs: tuple[str, ...]


def test_risk_dataclass_adapter_produces_scoped_risk_feature():
    risk = ExampleRiskPattern(
        source_type="risk_pattern",
        risk_id="risk-7",
        verified=True,
        observed_at=T0.isoformat(),
        assets=("vpn-gateway",),
        roles=("edge-firewall",),
        fault_family="credential_attack",
        config_version="netcfg-42",
        metric_window={"metric": "distinct_attack_sources", "aggregation": "count", "duration_seconds": 86400},
        pattern_type="distributed_password_spray",
        status="active",
        evidence_refs=("clickhouse:q-77",),
    )
    observation_row = observations_from_risk_pattern(risk)[0]
    assert observation_row.signal == "risk_pattern"
    assert observation_row.statement == "risk:distributed_password_spray"
    assert observation_row.scope.roles == ("edge-firewall",)
    assert observation_row.metric_window.duration_seconds == 86400


def test_native_real_risk_pattern_updates_engine_without_invented_verification_flag():
    risk_store = RiskPatternStore()
    pattern = risk_store.ingest(
        RiskEvent(
            event_id="event-1",
            observed_at=T0,
            risk_type="credential_attack",
            scope_key="account:mike",
            target_asset="vpn-gateway",
            source_ip="203.0.113.8",
            evidence_ref="clickhouse:event-1",
        )
    )
    assert pattern is not None

    engine = NetworkFeatureEngine()
    updated = engine.update(pattern, now=T0)
    assert len(updated) == 1
    feature = updated[0]
    assert feature.signal == "risk_pattern"
    assert feature.scope.asset_ids == ("vpn-gateway",)
    assert feature.scope.fault_family == "credential_attack"
    assert feature.scope.config_version == "unversioned"
    assert feature.metric_window.metric == "risk_event_count"
    assert feature.state == "candidate"


def test_native_replay_risk_is_not_allowed_to_train_production_feature():
    risk_store = RiskPatternStore()
    pattern = risk_store.ingest(
        RiskEvent(
            event_id="replay-1",
            observed_at=T0,
            risk_type="credential_attack",
            scope_key="account:mike",
            target_asset="vpn-gateway",
            provenance="replay",
        )
    )
    assert pattern is not None
    assert NetworkFeatureEngine().update(pattern, now=T0) == ()


def test_store_serialization_is_deterministic_and_round_trips():
    first = NetworkFeatureEngine()
    second = NetworkFeatureEngine()
    rows = [observation(f"inc-{index}", at=T0 + timedelta(minutes=index)) for index in range(3)]
    for row in rows:
        first.observe(row, now=row.observed_at)
    for row in reversed(rows):
        second.observe(row, now=row.observed_at)

    # Decision histories retain event order, so compare a snapshot round-trip
    # from one projection; every dictionary key and collection is canonical.
    dumped = first.store.dumps()
    assert dumped == FeatureStore.loads(dumped).dumps()
    assert json.loads(dumped)["schema_version"] == 1
    assert first.store.get(rows[0].feature_id) is not None

    # The final projected feature is independent of arrival order at the same
    # evidence cut, even though audit histories correctly describe each arrival.
    a = first.reassess(rows[0].feature_id, now=T0 + timedelta(hours=1)).to_dict()
    b = second.reassess(rows[0].feature_id, now=T0 + timedelta(hours=1)).to_dict()
    # Audit decision ids describe different arrival histories.  All projected
    # feature fields are nevertheless determined by the evidence cut.
    a.pop("decision_ids")
    b.pop("decision_ids")
    assert a == b


def test_observation_index_moves_revised_source_to_its_new_feature():
    store = FeatureStore()
    original = observation("inc-revised")
    revised = replace(
        original,
        statement="root_cause:revised_uplink_diagnosis",
        observed_at=T0 + timedelta(minutes=1),
    )

    assert store.upsert_observation(original) is True
    assert store.upsert_observation(revised) is True

    assert store.observations_for(original.feature_id) == ()
    assert store.observations_for(revised.feature_id) == (revised,)
    assert store.feature_ids() == (revised.feature_id,)


def test_ranking_returns_only_promoted_scope_compatible_features_and_audits_decay():
    engine = NetworkFeatureEngine()
    for index in range(3):
        engine.observe(observation(f"inc-{index}"), now=T0)

    matches = engine.rank_for_investigation(
        at=T0 + timedelta(hours=1),
        asset_ids=("switch-17",),
        roles=("access-switch",),
        fault_family="carrier_loss",
        config_version="netcfg-42",
    )
    assert len(matches) == 1
    assert matches[0].matched_on == ("asset", "role", "fault_family", "config_version")
    assert matches[0].feature.state == "promoted"
    assert matches[0].score > matches[0].feature.confidence

    assert engine.rank_for_investigation(
        at=T0 + timedelta(hours=1),
        asset_ids=("firewall-1",),
        fault_family="carrier_loss",
    ) == ()


def test_repeated_recall_same_day_does_not_create_unbounded_retention_decisions():
    engine = NetworkFeatureEngine()
    for index in range(3):
        engine.observe(observation(f"inc-{index}"), now=T0)
    feature_id = observation("inc-0").feature_id

    engine.rank_for_investigation(at=T0 + timedelta(hours=1), asset_ids=("switch-17",))
    after_first = len(engine.store.decisions_for(feature_id))
    engine.rank_for_investigation(
        at=T0 + timedelta(hours=1, seconds=10), asset_ids=("switch-17",)
    )

    assert len(engine.store.decisions_for(feature_id)) == after_first


def test_invalid_scope_metric_and_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="config_version"):
        FeatureScope(("a",), ("switch",), "carrier_loss", "")
    with pytest.raises(ValueError, match="duration_seconds"):
        MetricWindow("errors", "count", 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        observation("bad", at=datetime(2026, 8, 1, 12))
