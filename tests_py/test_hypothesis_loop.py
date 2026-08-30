"""Deterministic tests for observation-driven root-cause competition."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.investigate.hypothesis_loop import (
    EvidenceInput,
    HypothesisLoop,
    ProbeCandidate,
    RootCauseHypothesis,
)


T0 = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
ASSET = "device:camera-23"


def _hypothesis(hypothesis_id: str, statement: str | None = None) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement or hypothesis_id,
        entity_id=ASSET,
        valid_from=T0,
        valid_to=T0 + timedelta(hours=1),
        updated_at=T0,
    )


def _loop(*hypothesis_ids: str) -> HypothesisLoop:
    loop = HypothesisLoop.create("incident-001", at=T0)
    for hypothesis_id in hypothesis_ids:
        loop.add_hypothesis(_hypothesis(hypothesis_id))
    return loop


def _evidence(
    evidence_id: str,
    hypothesis_id: str,
    *,
    polarity: str = "supports",
    decisive: bool = False,
    source: str = "replay_fixture",
    entity_id: str = ASSET,
    observed_at: datetime = T0 + timedelta(minutes=10),
    collection_status: str = "observed",
    probe_id: str | None = None,
    resolves: tuple[str, ...] = (),
) -> EvidenceInput:
    return EvidenceInput(
        evidence_id=evidence_id,
        hypothesis_id=hypothesis_id,
        entity_id=entity_id,
        observed_at=observed_at,
        source=source,
        polarity=polarity,
        decisive=decisive,
        collection_status=collection_status,
        summary=f"observation {evidence_id}",
        probe_id=probe_id,
        resolves_evidence_ids=resolves,
    )


def test_same_symptom_different_causes_selects_probe_covering_most_live_hypotheses():
    loop = _loop("policy-block", "service-down", "route-loss")
    loop.add_probe(
        ProbeCandidate(
            probe_id="policy-counter",
            description="read matching firewall policy counter",
            target_entity_id=ASSET,
            distinguishes_hypothesis_ids=("policy-block", "service-down"),
            priority=10,
        ),
        at=T0,
    )
    loop.add_probe(
        ProbeCandidate(
            probe_id="end-to-end-path",
            description="observe policy, route and listener along the path",
            target_entity_id=ASSET,
            distinguishes_hypothesis_ids=("policy-block", "service-down", "route-loss"),
            priority=0,
            estimated_cost=2.0,
        ),
        at=T0,
    )
    loop.add_probe(
        ProbeCandidate(
            probe_id="listener-only",
            description="read target listener",
            target_entity_id=ASSET,
            distinguishes_hypothesis_ids=("service-down",),
            priority=100,
        ),
        at=T0,
    )

    selected = loop.select_next_probe(at=T0 + timedelta(minutes=1))

    assert selected is not None
    assert selected.probe_id == "end-to-end-path"
    assert selected.status == "selected"
    assert {item.status for item in loop.state.hypotheses} == {"testing"}


def test_probe_ranking_is_deterministic_after_coverage_priority_and_cost_ties():
    loop = _loop("a", "b")
    for probe_id in ("probe-b", "probe-a"):
        loop.add_probe(
            ProbeCandidate(
                probe_id=probe_id,
                description=probe_id,
                target_entity_id=ASSET,
                distinguishes_hypothesis_ids=("a", "b"),
                priority=3,
                estimated_cost=1.0,
            ),
            at=T0,
        )

    assert loop.select_next_probe(at=T0).probe_id == "probe-a"  # type: ignore[union-attr]


def test_current_decisive_observation_confirms_and_decisive_counter_observation_rejects():
    loop = _loop("service-down", "policy-block")

    loop.record_evidence(
        _evidence("ev-listener-absent", "service-down", decisive=True)
    )
    loop.record_evidence(
        _evidence(
            "ev-policy-zero-hits",
            "policy-block",
            polarity="opposes",
            decisive=True,
        )
    )

    assert loop.get_hypothesis("service-down").status == "confirmed"
    assert loop.get_hypothesis("policy-block").status == "rejected"


def test_conflicting_evidence_keeps_candidate_open_until_counter_observation_is_resolved():
    loop = _loop("policy-block")
    loop.record_evidence_batch(
        (
            _evidence("ev-policy-hit", "policy-block", decisive=True),
            _evidence(
                "ev-path-bypass",
                "policy-block",
                polarity="opposes",
                decisive=True,
            ),
        )
    )

    hypothesis = loop.get_hypothesis("policy-block")
    assert hypothesis.status == "testing"
    assert hypothesis.supporting_evidence_ids == ("ev-policy-hit",)
    assert hypothesis.opposing_evidence_ids == ("ev-path-bypass",)

    loop.record_evidence(
        _evidence(
            "ev-bypass-clock-was-wrong",
            "policy-block",
            decisive=True,
            resolves=("ev-path-bypass",),
            observed_at=T0 + timedelta(minutes=20),
        )
    )
    assert loop.get_hypothesis("policy-block").status == "confirmed"


def test_tool_failure_is_recorded_but_never_counts_as_counter_evidence():
    loop = _loop("service-down")
    loop.add_probe(
        ProbeCandidate(
            probe_id="read-listener",
            description="read the target listener",
            target_entity_id=ASSET,
            distinguishes_hypothesis_ids=("service-down",),
        ),
        at=T0,
    )
    loop.select_next_probe(at=T0 + timedelta(minutes=1))

    observation = loop.record_evidence(
        _evidence(
            "ev-command-timeout",
            "service-down",
            polarity="opposes",
            decisive=True,
            source="live_tool",
            collection_status="tool_failed",
            probe_id="read-listener",
        )
    )

    assert observation.collection_status == "tool_failed"
    assert loop.get_hypothesis("service-down").status == "testing"
    assert loop.get_hypothesis("service-down").opposing_evidence_ids == ()
    assert next(item for item in loop.state.probes if item.probe_id == "read-listener").status == "failed"


def test_wrong_entity_wrong_time_and_historical_memory_cannot_confirm():
    loop = _loop("service-down")
    loop.record_evidence(
        _evidence(
            "ev-old-memory",
            "service-down",
            decisive=True,
            source="historical_memory",
        )
    )
    loop.record_evidence(
        _evidence(
            "ev-other-device",
            "service-down",
            decisive=True,
            source="telemetry",
            entity_id="device:camera-99",
        )
    )
    loop.record_evidence(
        _evidence(
            "ev-outside-window",
            "service-down",
            decisive=True,
            source="telemetry",
            observed_at=T0 + timedelta(hours=2),
        )
    )

    assert loop.get_hypothesis("service-down").status == "testing"

    loop.record_evidence(
        _evidence(
            "ev-current-listener",
            "service-down",
            decisive=True,
            source="telemetry",
            observed_at=T0 + timedelta(minutes=30),
        )
    )
    assert loop.get_hypothesis("service-down").status == "confirmed"


def test_snapshot_restore_continues_monotonic_versions_and_evidence_sequence():
    loop = _loop("route-loss")
    loop.record_evidence(
        _evidence(
            "ev-similar-history",
            "route-loss",
            source="historical_memory",
        )
    )
    before_version = loop.state.state_version
    before_json = loop.dump_json()

    restored = HypothesisLoop.restore(before_json)

    assert restored.dump_json() == before_json
    restored.record_evidence(
        _evidence(
            "ev-route-missing",
            "route-loss",
            decisive=True,
            source="live_tool",
            observed_at=T0 + timedelta(minutes=15),
        )
    )
    assert restored.state.state_version == before_version + 1
    assert [item.sequence for item in restored.state.evidence] == [1, 2]
    assert restored.get_hypothesis("route-loss").status == "confirmed"


def test_non_decisive_current_reading_cannot_confirm_a_root_cause():
    loop = _loop("route-loss")
    loop.record_evidence(_evidence("ev-route-suspicious", "route-loss"))

    assert loop.get_hypothesis("route-loss").status == "testing"


def test_named_probe_selection_supports_a_requested_read():
    loop = _loop("route-loss")
    loop.add_probe(ProbeCandidate(
        probe_id="probe-route",
        description="ip route show",
        target_entity_id="device:camera-1",
        distinguishes_hypothesis_ids=("route-loss",),
    ), at=T0)

    selected = loop.select_probe("probe-route", at=T0 + timedelta(seconds=1))

    assert selected.status == "selected"
    assert loop.get_hypothesis("route-loss").status == "testing"
    with pytest.raises(ValueError, match="not available"):
        loop.select_probe("probe-route", at=T0 + timedelta(seconds=2))
