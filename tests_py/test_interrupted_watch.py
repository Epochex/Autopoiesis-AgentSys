"""Startup reconciliation for actions abandoned inside their watch window."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.remediate import recurrence
from core.remediate.sentinel import Detection, Sentinel, timeline
from domains.network_rca.incident_memory import synthesize_incident_run


@pytest.fixture(autouse=True)
def _own_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(tmp_path / "timeline.jsonl"))


def _write(events: list[dict]) -> None:
    from core.remediate.sentinel import _default_timeline

    path: Path = _default_timeline()
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _open_chain() -> list[dict]:
    return [
        {
            "at": "2026-08-23T00:17:20+00:00",
            "kind": "detected",
            "detector": "failed_units",
            "subject": "demo-api.service",
            "target": "demo-api.service",
            "action": "restart_unit",
        },
        {
            "at": "2026-08-23T00:17:22+00:00",
            "kind": "remediation_committed",
            "subject": "demo-api.service",
            "action": "restart_unit",
        },
        {
            "at": "2026-08-23T00:17:23+00:00",
            "kind": "bakein_opened",
            "subject": "demo-api.service",
            "action": "restart_unit",
        },
        {
            "at": "2026-08-23T00:17:39+00:00",
            "kind": "bakein_sampled",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "probe": "gateway",
            "healthy": True,
        },
    ]


def _sentinel(*, now: float = 100.0, cooldown: float = 600.0):
    calls: list[tuple[str, str]] = []

    def execute(action, target, on_command=None):
        calls.append((action, target))
        return {"verdict": {"outcome": "passed", "samples": []}}

    sentinel = Sentinel(
        detectors=[],
        execute=execute,
        preflight=lambda action, target, on_command=None: {"eligible": True},
        confirm_polls=1,
        cooldown_sec=cooldown,
        clock=lambda: now,
    )
    return sentinel, calls


def test_committed_watch_is_closed_with_complete_interruption_event():
    _write(_open_chain())
    sentinel, _calls = _sentinel()

    written = sentinel.reconcile_interrupted_watches()

    assert len(written) == 1
    event = written[0]
    assert event["kind"] == "watch_interrupted"
    assert event["subject"] == "demo-api.service"
    assert event["detector"] == "failed_units"
    assert event["action"] == "restart_unit"
    assert event["committed_at"] == "2026-08-23T00:17:22+00:00"
    assert event["last_sample_at"] == "2026-08-23T00:17:39+00:00"
    assert event["samples_seen"] == 1
    assert event["note"] == (
        "动作已执行但观察期没走完（进程在中途重启）。"
        "这次改动没有被回读验证过，需要人确认它站住了没有。"
    )


def test_opened_watch_without_a_commit_row_is_still_reconciled():
    events = [event for event in _open_chain() if event["kind"] != "remediation_committed"]
    events = [event for event in events if event["kind"] != "bakein_sampled"]
    _write(events)
    sentinel, _calls = _sentinel()

    written = sentinel.reconcile_interrupted_watches()

    assert len(written) == 1
    assert written[0]["committed_at"] is None
    assert written[0]["last_sample_at"] is None
    assert written[0]["samples_seen"] == 0


@pytest.mark.parametrize(
    "terminal",
    [
        "remediated",
        "resolved",
        "bakein_passed",
        "bakein_regressed",
        "remediation_reverted",
    ],
)
def test_closed_watch_is_never_reported_as_interrupted(terminal: str):
    events = _open_chain()
    events.append({
        "at": "2026-08-23T00:18:00+00:00",
        "kind": terminal,
        "subject": "demo-api.service",
        "detector": "failed_units",
        "action": "restart_unit",
    })
    events.append({"at": "2026-08-23T00:18:01+00:00", "kind": "sentinel_started"})
    _write(events)
    sentinel, _calls = _sentinel()

    assert sentinel.reconcile_interrupted_watches() == []
    assert not [event for event in timeline(None) if event["kind"] == "watch_interrupted"]


def test_two_restarts_append_one_interruption_and_restore_its_cooldown():
    _write([
        *_open_chain(),
        {"at": "2026-08-23T00:17:51+00:00", "kind": "sentinel_started"},
    ])
    first, _calls = _sentinel(now=100.0)
    second, _calls = _sentinel(now=200.0)

    assert len(first.reconcile_interrupted_watches()) == 1
    assert second.reconcile_interrupted_watches() == []
    assert len([event for event in timeline(None) if event["kind"] == "watch_interrupted"]) == 1
    assert second._cooldown_until["failed_units:demo-api.service"] > 200.0


def test_interrupted_target_cools_before_another_action():
    _write(_open_chain())
    sentinel, calls = _sentinel(now=100.0)
    sentinel.detectors = [lambda: [Detection(
        detector="failed_units",
        family="fam-perception-selfheal",
        subject="demo-api.service",
        severity="high",
        summary="still failed",
        action="restart_unit",
        target="demo-api.service",
    )]]

    sentinel.reconcile_interrupted_watches()
    sentinel.poll_once()

    assert calls == []
    assert any(event["kind"] == "cooldown" for event in timeline(None))


def test_gateway_reconciles_before_starting_the_polling_thread(monkeypatch):
    from frontend.gateway.app import sentinel_wiring

    order: list[str] = []

    class FakeSentinel:
        def reconcile_interrupted_watches(self):
            order.append("reconciled")

        def run_forever(self):
            order.append("polled")

    class InlineThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            order.append("thread_started")
            self.target()

    monkeypatch.setenv("AUTOPOIESIS_SENTINEL", "1")
    monkeypatch.setattr(sentinel_wiring, "get_sentinel", lambda: FakeSentinel())
    monkeypatch.setattr(sentinel_wiring.threading, "Thread", InlineThread)

    sentinel_wiring.start_background()

    assert order == ["reconciled", "thread_started", "polled"]


def test_interruption_is_neither_a_recurrence_nor_positive_memory():
    events = [
        *_open_chain(),
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "watch_interrupted",
            "subject": "demo-api.service",
            "detector": "failed_units",
            "action": "restart_unit",
            "committed_at": "2026-08-23T00:17:22+00:00",
            "last_sample_at": "2026-08-23T00:17:39+00:00",
            "samples_seen": 1,
        },
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "detected",
            "subject": "demo-api.service",
            "detector": "failed_units",
            "action": "restart_unit",
        },
    ]

    projected = recurrence.project(events, now=datetime.now(timezone.utc).timestamp())
    history = projected.get("failed_units:demo-api.service:restart_unit")
    assert history is None or history.recurrences == 0
    assert synthesize_incident_run(events) is None
