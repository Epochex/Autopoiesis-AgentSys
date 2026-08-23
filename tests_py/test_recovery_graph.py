from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.remediate.recovery_graph import (
    ActionNode,
    DuplicateIncidentError,
    FreshEvidence,
    InvalidGraphError,
    InvalidTransitionError,
    RecoveryEdge,
    RecoveryGraph,
    UnknownActionError,
)


UTC = timezone.utc
T0 = datetime(2026, 8, 23, 10, tzinfo=UTC)
METRICS = {"packet_loss": 0.0, "management_ping": 1}


def node(
    action_id: str,
    mechanism: str,
    *,
    impact: int = 2,
) -> ActionNode:
    return ActionNode(
        action_id=action_id,
        mechanism=mechanism,
        impact=impact,
        required_metrics=("packet_loss", "management_ping"),
        rollback_id=f"undo:{action_id}",
    )


A = node("bounce_uplink", "interface_cycle", impact=2)
B = node("reload_route", "route_refresh", impact=2)
C = node("renew_lease", "address_renewal", impact=1)
AB = RecoveryEdge("bounce_uplink", "reload_route", ("route_still_stale",))
BC = RecoveryEdge("reload_route", "renew_lease", ("lease_expired",))


def graph(*, include_c: bool = False, max_actions: int = 2) -> RecoveryGraph:
    actions = (A, B, C) if include_c else (A, B)
    edges = (AB, BC) if include_c else (AB,)
    return RecoveryGraph(
        actions,
        edges,
        max_actions_per_incident=max_actions,
        evidence_max_age=timedelta(minutes=5),
    )


def open_and_commit(value: RecoveryGraph, incident: str = "inc-1") -> None:
    value.open_incident(incident, A.action_id, at=T0)
    value.precheck(
        incident,
        at=T0 + timedelta(seconds=1),
        metrics=METRICS,
        management_reachable=True,
    )
    value.commit(
        incident,
        at=T0 + timedelta(seconds=2),
        management_reachable=True,
        readback_passed=True,
    )


def revert_a(value: RecoveryGraph, incident: str = "inc-1") -> None:
    open_and_commit(value, incident)
    value.begin_fast_observation(
        incident,
        at=T0 + timedelta(seconds=3),
        metrics=METRICS,
        management_reachable=True,
    )
    value.revert(
        incident,
        at=T0 + timedelta(seconds=4),
        rollback_succeeded=True,
        rollback_metrics=METRICS,
        management_reachable=True,
    )


def test_full_single_action_state_progression_is_explicit_and_audited():
    value = graph()
    run = value.open_incident("inc-1", A.action_id, at=T0)
    assert run.state == "proposed"

    assert value.precheck(
        "inc-1", at=T0 + timedelta(seconds=1), metrics=METRICS, management_reachable=True
    ).state == "prechecked"
    assert value.commit(
        "inc-1",
        at=T0 + timedelta(seconds=2),
        management_reachable=True,
        readback_passed=True,
    ).state == "committed"
    assert value.begin_fast_observation(
        "inc-1",
        at=T0 + timedelta(seconds=3),
        metrics=METRICS,
        management_reachable=True,
    ).state == "fast_observing"
    assert value.begin_stability_observation(
        "inc-1",
        at=T0 + timedelta(seconds=4),
        metrics=METRICS,
        management_reachable=True,
        fast_window_passed=True,
    ).state == "stability_observing"
    assert value.pass_stability(
        "inc-1",
        at=T0 + timedelta(seconds=5),
        metrics=METRICS,
        management_reachable=True,
        stability_window_passed=True,
    ).state == "passed"

    run = value.get_run("inc-1")
    assert run is not None
    assert [event.to_state for event in run.transitions] == [
        "proposed",
        "prechecked",
        "committed",
        "fast_observing",
        "stability_observing",
        "passed",
    ]
    assert [event.sequence for event in run.transitions] == list(range(1, 7))
    assert len({event.transition_id for event in run.transitions}) == 6


def test_second_action_requires_verified_revert_fresh_edge_evidence_and_budget():
    value = graph()
    revert_a(value)
    evidence = FreshEvidence(
        evidence_id="ev-22",
        observed_at=T0 + timedelta(seconds=5),
        facts=("route_still_stale", "unrelated_fact"),
    )
    run = value.propose_next(
        "inc-1",
        B.action_id,
        at=T0 + timedelta(seconds=6),
        evidence=(evidence,),
    )

    assert run.state == "proposed"
    assert run.current_action_id == B.action_id
    assert run.attempted_action_ids == [A.action_id, B.action_id]
    assert run.budget_remaining == 0
    transition = run.transitions[-1]
    assert transition.from_state == "reverted"
    assert transition.reason == "fresh_evidence_selected_registered_fallback"
    assert transition.details["evidence_ids"] == ["ev-22"]
    assert transition.details["matched_facts"] == ["route_still_stale"]


def test_default_two_action_budget_escalates_before_a_third_action():
    value = graph(include_c=True)
    revert_a(value)
    value.propose_next(
        "inc-1",
        B.action_id,
        at=T0 + timedelta(seconds=5),
        evidence=(
            FreshEvidence("ev-ab", T0 + timedelta(seconds=5), ("route_still_stale",)),
        ),
    )
    value.precheck(
        "inc-1", at=T0 + timedelta(seconds=6), metrics=METRICS, management_reachable=True
    )
    value.commit(
        "inc-1",
        at=T0 + timedelta(seconds=7),
        management_reachable=True,
        readback_passed=True,
    )
    value.revert(
        "inc-1",
        at=T0 + timedelta(seconds=8),
        rollback_succeeded=True,
        rollback_metrics=METRICS,
        management_reachable=True,
    )
    run = value.propose_next(
        "inc-1",
        C.action_id,
        at=T0 + timedelta(seconds=9),
        evidence=(FreshEvidence("ev-bc", T0 + timedelta(seconds=9), ("lease_expired",)),),
    )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "action_budget_exhausted"
    assert run.attempted_action_ids == [A.action_id, B.action_id]


def test_stale_or_non_matching_evidence_escalates_instead_of_guessing():
    stale_graph = graph()
    revert_a(stale_graph)
    stale = FreshEvidence("old", T0 - timedelta(hours=1), ("route_still_stale",))
    run = stale_graph.propose_next(
        "inc-1", B.action_id, at=T0 + timedelta(seconds=5), evidence=(stale,)
    )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "fresh_evidence_not_satisfied"
    assert run.transitions[-1].details["missing_facts"] == ["route_still_stale"]

    mismatch_graph = graph()
    revert_a(mismatch_graph)
    mismatch = FreshEvidence("new", T0 + timedelta(seconds=5), ("arp_owner_changed",))
    mismatch_run = mismatch_graph.propose_next(
        "inc-1", B.action_id, at=T0 + timedelta(seconds=6), evidence=(mismatch,)
    )
    assert mismatch_run.state == "escalated"
    assert mismatch_run.transitions[-1].reason == "fresh_evidence_not_satisfied"


def test_unknown_action_is_rejected_without_mutating_incident_audit():
    value = graph()
    with pytest.raises(UnknownActionError):
        value.open_incident("inc-unknown", "invented_action", at=T0)
    assert value.get_run("inc-unknown") is None

    revert_a(value)
    before = value.snapshot_json()
    with pytest.raises(UnknownActionError):
        value.propose_next(
            "inc-1",
            "invented_action",
            at=T0 + timedelta(seconds=5),
            evidence=(),
        )
    assert value.snapshot_json() == before


def test_graph_rejects_unknown_edges_cycles_same_mechanism_and_higher_impact():
    with pytest.raises(UnknownActionError):
        RecoveryGraph((A,), (RecoveryEdge(A.action_id, "invented", ("fact",)),))

    equal_a = node("a", "mechanism-a", impact=1)
    equal_b = node("b", "mechanism-b", impact=1)
    with pytest.raises(InvalidGraphError, match="acyclic"):
        RecoveryGraph(
            (equal_a, equal_b),
            (
                RecoveryEdge("a", "b", ("to-b",)),
                RecoveryEdge("b", "a", ("to-a",)),
            ),
        )

    same_mechanism = node("same", A.mechanism, impact=A.impact)
    with pytest.raises(InvalidGraphError, match="repeats mechanism"):
        RecoveryGraph((A, same_mechanism), (RecoveryEdge(A.action_id, "same", ("fact",)),))

    higher = node("higher", "different", impact=A.impact + 1)
    with pytest.raises(InvalidGraphError, match="increases impact"):
        RecoveryGraph((A, higher), (RecoveryEdge(A.action_id, "higher", ("fact",)),))


@pytest.mark.parametrize(
    ("metrics", "missing"),
    [
        ({"management_ping": 1}, "packet_loss"),
        ({"packet_loss": float("nan"), "management_ping": 1}, "packet_loss"),
        ({"packet_loss": 0.0}, "management_ping"),
    ],
)
def test_missing_or_non_finite_precheck_metrics_escalate(metrics, missing):
    value = graph()
    value.open_incident("inc-1", A.action_id, at=T0)
    run = value.precheck(
        "inc-1",
        at=T0 + timedelta(seconds=1),
        metrics=metrics,
        management_reachable=True,
    )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "required_metrics_missing"
    assert missing in run.transitions[-1].details["missing_metrics"]


def test_missing_metrics_during_observation_escalate():
    value = graph()
    open_and_commit(value)
    run = value.begin_fast_observation(
        "inc-1",
        at=T0 + timedelta(seconds=3),
        metrics={"packet_loss": 0.0},
        management_reachable=True,
    )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "required_metrics_missing"
    assert run.transitions[-1].details["gate"] == "fast"


@pytest.mark.parametrize("gate", ["precheck", "commit", "observe", "rollback"])
def test_management_path_loss_always_escalates(gate):
    value = graph()
    value.open_incident("inc-1", A.action_id, at=T0)
    if gate == "precheck":
        run = value.precheck(
            "inc-1", at=T0, metrics=METRICS, management_reachable=False
        )
    else:
        value.precheck("inc-1", at=T0, metrics=METRICS, management_reachable=True)
        if gate == "commit":
            run = value.commit(
                "inc-1", at=T0, management_reachable=False, readback_passed=True
            )
        else:
            value.commit("inc-1", at=T0, management_reachable=True, readback_passed=True)
            if gate == "observe":
                run = value.begin_fast_observation(
                    "inc-1", at=T0, metrics=METRICS, management_reachable=False
                )
            else:
                run = value.revert(
                    "inc-1",
                    at=T0,
                    rollback_succeeded=True,
                    rollback_metrics=METRICS,
                    management_reachable=False,
                )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "management_path_unreachable"


def test_rollback_failure_and_missing_rollback_readback_escalate():
    failed = graph()
    open_and_commit(failed)
    failed_run = failed.revert(
        "inc-1",
        at=T0 + timedelta(seconds=3),
        rollback_succeeded=False,
        rollback_metrics=METRICS,
        management_reachable=True,
    )
    assert failed_run.state == "escalated"
    assert failed_run.transitions[-1].reason == "rollback_failed"

    missing = graph()
    open_and_commit(missing)
    missing_run = missing.revert(
        "inc-1",
        at=T0 + timedelta(seconds=3),
        rollback_succeeded=True,
        rollback_metrics={"packet_loss": 0.0},
        management_reachable=True,
    )
    assert missing_run.state == "escalated"
    assert missing_run.transitions[-1].reason == "rollback_metrics_missing"


def test_unregistered_edge_escalates_and_does_not_add_requested_action():
    value = RecoveryGraph((A, B), (), max_actions_per_incident=2)
    revert_a(value)
    run = value.propose_next(
        "inc-1",
        B.action_id,
        at=T0 + timedelta(seconds=5),
        evidence=(FreshEvidence("ev", T0, ("route_still_stale",)),),
    )
    assert run.state == "escalated"
    assert run.transitions[-1].reason == "fallback_edge_not_registered"
    assert run.attempted_action_ids == [A.action_id]


def test_snapshot_restore_is_deterministic_and_can_continue_state_machine():
    value = graph()
    value.open_incident("z-incident", A.action_id, at=T0)
    value.precheck(
        "z-incident", at=T0, metrics=METRICS, management_reachable=True
    )
    value.open_incident("a-incident", A.action_id, at=T0)

    snapshot = value.snapshot_json()
    restored = RecoveryGraph.restore(snapshot)
    assert restored.snapshot_json() == snapshot
    decoded = json.loads(snapshot)
    assert [row["incident_id"] for row in decoded["runs"]] == ["a-incident", "z-incident"]
    assert [row["action_id"] for row in decoded["actions"]] == sorted(
        row["action_id"] for row in decoded["actions"]
    )

    continued = restored.commit(
        "z-incident",
        at=T0 + timedelta(seconds=1),
        management_reachable=True,
        readback_passed=True,
    )
    assert continued.state == "committed"
    assert continued.transitions[-1].sequence == 3


def test_snapshot_rejects_unknown_attempted_action_and_broken_sequence():
    value = graph()
    value.open_incident("inc-1", A.action_id, at=T0)
    snapshot = value.snapshot()
    snapshot["runs"][0]["attempted_action_ids"] = ["invented"]
    with pytest.raises(UnknownActionError):
        RecoveryGraph.restore(snapshot)

    sequence_snapshot = value.snapshot()
    sequence_snapshot["runs"][0]["transitions"][0]["sequence"] = 2
    with pytest.raises(ValueError, match="not contiguous"):
        RecoveryGraph.restore(sequence_snapshot)


def test_invalid_state_skips_and_duplicate_incident_are_rejected():
    value = graph()
    value.open_incident("inc-1", A.action_id, at=T0)
    with pytest.raises(DuplicateIncidentError):
        value.open_incident("inc-1", A.action_id, at=T0)
    with pytest.raises(InvalidTransitionError):
        value.commit(
            "inc-1", at=T0, management_reachable=True, readback_passed=True
        )
    with pytest.raises(InvalidTransitionError):
        value.begin_stability_observation(
            "inc-1",
            at=T0,
            metrics=METRICS,
            management_reachable=True,
            fast_window_passed=True,
        )


def test_regressed_window_cannot_be_marked_as_stable_or_passed():
    value = graph()
    open_and_commit(value)
    value.begin_fast_observation(
        "inc-1", at=T0, metrics=METRICS, management_reachable=True
    )
    with pytest.raises(InvalidTransitionError, match="must be reverted"):
        value.begin_stability_observation(
            "inc-1",
            at=T0,
            metrics=METRICS,
            management_reachable=True,
            fast_window_passed=False,
        )
    assert value.get_run("inc-1").state == "fast_observing"  # type: ignore[union-attr]


def test_action_and_evidence_models_require_safety_metadata():
    with pytest.raises(ValueError, match="required metrics"):
        ActionNode("unsafe", "restart", 1, (), "undo")
    with pytest.raises(ValueError, match="rollback_id"):
        ActionNode("unsafe", "restart", 1, ("health",), "")
    with pytest.raises(ValueError, match="fresh evidence"):
        RecoveryEdge("a", "b", ())
    with pytest.raises(ValueError, match="facts"):
        FreshEvidence("ev", T0, ())
    with pytest.raises(ValueError, match="timezone-aware"):
        FreshEvidence("ev", datetime(2026, 8, 23, 10), ("fact",))
