"""Falsifiable tests for the streaming fault-injection self-heal benchmark.

These verify that (1) the scored trajectory is the REAL offline evolving run — its
warm accuracy equals the authoritative /api/rca/evolution delta; (2) each case's
replay burst is real-schema-shaped and clearly tagged; and (3) Redpanda production
    degrades gracefully when the Kafka endpoint is absent, as in CI/sandbox.
"""
from __future__ import annotations

from core.evolve.replay_stream import (
    _SCHEMA_FIELDS,
    _load_cases,
    case_fault_events,
    produce_replay,
    replay_trajectory,
    topic_status,
)
from frontend.gateway.app.rca_reader import load_evolution

import pytest

# The replay benchmark runs against the local-only held-out cases (gitignored).
# Absent them — as on CI — skip the whole module rather than fail; every
# assertion below still runs on the box that holds the real dataset.
_cases, _gt = _load_cases()
if not _cases:
    pytest.skip(
        "real held-out dataset not present (local-only, gitignored)",
        allow_module_level=True,
    )


def test_trajectory_is_the_real_evolution_run():
    traj = replay_trajectory(passes=4)
    assert traj["ready"] is True

    per_event = traj["per_event"]
    assert per_event, "trajectory must expose per-event rows"
    for row in per_event:
        # correct/passed are scored ints straight off the real run
        assert row["correct"] in (0, 1)
        assert row["passed"] in (0, 1)
        assert isinstance(row["probes"], int)
        assert set(row) >= {"i", "pass", "case", "correct", "passed", "probes", "retrieved", "shortcut", "memory"}

    summary = traj["summary"]
    # the headline number must match the authoritative evolution endpoint verbatim
    evolution = load_evolution(None, 4)
    assert summary["accuracy_warm"] == evolution["delta"]["accuracy_warm"]
    assert summary["accuracy_cold"] == evolution["delta"]["accuracy_cold"]
    assert summary["probes_warm"] == evolution["delta"]["probes_warm"]
    assert summary["memory_grown"] == evolution["delta"]["memory_grown"]
    assert summary["n_cases"] == evolution["nCases"]

    # self-heal scorecard is derived from the same per-event rows
    heal = traj["self_heal"]
    assert heal["events_scored"] == len(per_event)
    assert heal["detected"] == sum(r["correct"] for r in per_event)
    assert heal["verified"] == sum(r["passed"] for r in per_event)
    assert 0.0 <= heal["heal_rate"] <= 1.0


def test_case_fault_events_are_real_schema_tagged_replay():
    cases, _gt = _load_cases()
    assert cases, "real held-out cases must load"
    schema = set(_SCHEMA_FIELDS)
    for case in cases:
        events = case_fault_events(case)
        assert len(events) >= 5, f"{case.id} must yield >=5 events, got {len(events)}"
        for ev in events:
            assert ev["replay"] is True
            assert ev["case_id"] == case.id
            # every real facts.raw schema key is present
            assert schema.issubset(ev.keys()), f"{case.id} missing schema keys: {schema - set(ev)}"
            # never sent to prod: these are replay-tagged demo signals only
            assert isinstance(ev["dstport"], int)


def test_admin_bruteforce_events_carry_login_failed_signal():
    cases, _gt = _load_cases()
    admin = next(c for c in cases if "bruteforce" in c.id)
    events = case_fault_events(admin)
    assert any(ev["action"] == "login-failed" and ev["subtype"] == "admin" for ev in events)
    assert any(ev["action"] == "login-disabled" for ev in events)  # the lockout tail


def test_produce_replay_degrades_gracefully_without_kafka_endpoint(monkeypatch):
    monkeypatch.setattr("core.evolve.replay_stream._KAFKA_BROKERS", "")
    result = produce_replay()  # must NOT raise
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["produced"] == 0
    assert result["topic"] == "autopoiesis.events.replay.v1"
    assert "not configured" in result["note"]


def test_topic_status_degrades_gracefully_without_kafka_endpoint(monkeypatch):
    monkeypatch.setattr("core.evolve.replay_stream._KAFKA_BROKERS", "")
    status = topic_status()  # must NOT raise
    assert status["degraded"] is True
    assert status["events"] is None
