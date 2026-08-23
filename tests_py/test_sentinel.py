"""The autonomous loop, and the branches where it declines to act.

An autonomous remediator's dangerous failure is not a wrong action; it is the
same action repeated forever, or an action taken on one noisy sample. Every
test here pins one of the brakes.
"""

from __future__ import annotations

import threading

import pytest

from core.remediate.sentinel import Detection, Sentinel, timeline


@pytest.fixture(autouse=True)
def _own_timeline(tmp_path, monkeypatch):
    """One timeline per test.

    The loop now READS the timeline to count recurrences, so the shared
    pytest-wide temp file made every test inherit the resolutions of the ones
    before it — and a few tests inherited enough to trip the escalation ladder.
    Isolation was optional while the timeline was write-only; it is not now.
    """
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(tmp_path / "timeline.jsonl"))


def _detection(subject="demo.service", action="restart_unit"):
    return Detection(detector="d", family="f", subject=subject, severity="high",
                     summary="s", action=action, target=subject)


def _sentinel(detections, *, eligible=True, outcome="passed", clock=None):
    calls = {"preflight": [], "execute": [], "logged": []}

    # Both take a `log` sink the sentinel uses to stream each command it runs
    # into the timeline; the doubles accept it so the contract stays honest.
    def preflight(action, target, on_command=None):
        calls["preflight"].append((action, target))
        if on_command is not None:
            on_command({"argv": ["systemctl", "show", target], "rc": 0, "out": "", "truncated": False})
            calls["logged"].append("preflight")
        return {"eligible": eligible, "reason": "" if eligible else "preconditions not met"}

    def execute(action, target, on_command=None):
        calls["execute"].append((action, target))
        if on_command is not None:
            on_command({"argv": ["systemctl", "restart", target], "rc": 0, "out": "", "truncated": False})
            calls["logged"].append("execute")
        return {"ran": True, "needs_human": False, "verdict": {"outcome": outcome, "samples": []}}

    sentinel = Sentinel(
        detectors=[lambda: list(detections)],
        execute=execute, preflight=preflight, confirm_polls=2,
        clock=clock or (lambda: 0.0),
    )
    return sentinel, calls


def test_one_sample_never_triggers_an_action():
    """A single poll can catch a service mid-restart during a deploy."""
    sentinel, calls = _sentinel([_detection()])
    sentinel.poll_once()
    assert calls["execute"] == []


def test_a_confirmed_detection_is_acted_on():
    sentinel, calls = _sentinel([_detection()])
    sentinel.poll_once()
    sentinel.poll_once()
    assert calls["execute"] == [("restart_unit", "demo.service")]


def test_a_condition_that_clears_resets_its_streak():
    """An intermittent fault must re-confirm, not accumulate across hours."""
    findings: list[Detection] = [_detection()]
    sentinel = Sentinel(detectors=[lambda: list(findings)],
                        execute=lambda a, t, on_command=None: {"verdict": {"outcome": "passed"}},
                        preflight=lambda a, t, on_command=None: {"eligible": True}, confirm_polls=2)
    sentinel.poll_once()
    findings.clear()
    sentinel.poll_once()
    findings.append(_detection())
    sentinel.poll_once()
    assert sentinel._streak["d:demo.service"] == 1, "the streak must have restarted"


def test_a_detection_with_no_safe_action_is_reported_not_improvised_on():
    finding = _detection(action=None)
    finding.candidate_action = "temporary_firewall_block"
    finding.safety_reason = "missing TTL and rollback"
    sentinel, calls = _sentinel([finding])
    sentinel.poll_once()
    sentinel.poll_once()
    assert calls["execute"] == []
    assert calls["preflight"] == []
    event = next(row for row in timeline() if row["kind"] == "no_safe_action")
    assert event["candidate_action"] == "temporary_firewall_block"
    assert event["decision"] == "retained_not_executed"
    assert event["reason"] == "missing TTL and rollback"


def test_a_refused_preflight_stops_the_action():
    sentinel, calls = _sentinel([_detection()], eligible=False)
    sentinel.poll_once()
    sentinel.poll_once()
    assert calls["preflight"], "it must have asked"
    assert calls["execute"] == [], "and must not have acted"


def test_a_target_that_did_not_recover_goes_on_cooldown():
    """Repeating the same action forever is the failure mode that matters."""
    now = {"t": 0.0}
    sentinel, calls = _sentinel([_detection()], outcome="revert_unverified",
                                clock=lambda: now["t"])
    sentinel.poll_once()
    sentinel.poll_once()
    assert len(calls["execute"]) == 1
    for _ in range(5):
        now["t"] += 30
        sentinel.poll_once()
    assert len(calls["execute"]) == 1, "cooldown must suppress the repeat"


def test_a_fix_that_did_not_hold_buys_a_longer_wait_than_the_first_one():
    """The cooldown used to be cleared on success, so recurrence was invisible.

    A fault that is repaired and returns is the signal this whole ladder exists
    for; wiping the timer on the way out threw it away. The quiet period now
    doubles each time the same repair fails to hold.
    """
    now = {"t": 0.0}
    sentinel, calls = _sentinel([_detection()], outcome="passed", clock=lambda: now["t"])
    sentinel.poll_once(); sentinel.poll_once()
    assert len(calls["execute"]) == 1
    first_wait = sentinel._cooldown_until["d:demo.service"] - now["t"]

    # It came back: past the first cooldown, the second repair runs...
    now["t"] += first_wait + 1
    sentinel.poll_once(); sentinel.poll_once()
    assert len(calls["execute"]) == 2
    second_wait = sentinel._cooldown_until["d:demo.service"] - now["t"]

    assert second_wait > first_wait, "a repair that did not hold must buy a longer wait"


def test_a_repair_that_keeps_not_holding_is_refused_and_handed_over():
    """The point of the ladder: stop, and say who has to look at it.

    Restarting something that keeps breaking does not fix it — it hides the
    trend, which is the failure Bainbridge (1983) called automation camouflage.
    """
    from core.remediate.sentinel import timeline

    now = {"t": 0.0}
    sentinel, calls = _sentinel([_detection()], outcome="passed", clock=lambda: now["t"])
    for _ in range(6):
        sentinel.poll_once(); sentinel.poll_once()
        now["t"] += 4 * 3600      # past any rung of the doubling ladder

    escalations = [e for e in timeline(400) if e["kind"] == "escalated"]
    assert escalations, "it must eventually refuse rather than repair forever"
    first = escalations[0]
    assert first["recurrences"] >= 3
    assert first["subject"] == "demo.service"
    # the chain of prior repairs is the answer to "why did it stop?"
    assert len(first["prior_cycles"]) == first["recurrences"]
    assert all(c["outcome"] == "passed" for c in first["prior_cycles"])
    # and it is announced once, not every poll
    assert len(escalations) == 1


def test_an_escalated_target_is_not_acted_on_again():
    now = {"t": 0.0}
    sentinel, calls = _sentinel([_detection()], outcome="passed", clock=lambda: now["t"])
    for _ in range(6):
        sentinel.poll_once(); sentinel.poll_once()
        now["t"] += 4 * 3600
    settled = len(calls["execute"])
    for _ in range(4):
        sentinel.poll_once(); sentinel.poll_once()
        now["t"] += 4 * 3600
    assert len(calls["execute"]) == settled, "escalation means stop, not slow down"


def test_a_broken_detector_does_not_stop_the_others():
    def explodes():
        raise RuntimeError("detector bug")

    sentinel = Sentinel(
        detectors=[explodes, lambda: [_detection()]],
        execute=lambda a, t, on_command=None: {"verdict": {"outcome": "passed"}},
        preflight=lambda a, t, on_command=None: {"eligible": True}, confirm_polls=1,
    )
    result = sentinel.poll_once()
    assert len(result["detections"]) == 1


def test_a_manual_poll_cannot_overlap_the_background_cycle():
    entered = threading.Event()
    release = threading.Event()

    def slow_detector():
        entered.set()
        assert release.wait(timeout=2)
        return []

    sentinel, _calls = _sentinel([])
    sentinel.detectors = [slow_detector]
    worker = threading.Thread(target=sentinel.poll_once)
    worker.start()
    assert entered.wait(timeout=1)

    duplicate = sentinel.poll_once(blocking=False)
    assert duplicate == {"detections": [], "acted": [], "busy": True}

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_an_operator_poll_can_select_one_detector():
    calls: list[str] = []

    def unrelated():
        calls.append("unrelated")
        return []

    def requested():
        calls.append("requested")
        return [_detection(action=None)]

    sentinel, _ = _sentinel([])
    sentinel.detectors = [unrelated, requested]
    result = sentinel.poll_once([requested])
    assert calls == ["requested"]
    assert result["detections"][0]["subject"] == "demo.service"
    assert result["busy"] is False


def test_the_background_loop_is_off_unless_enabled(monkeypatch):
    """Something that acts on its own must not arrive as a deploy side effect."""
    from frontend.gateway.app import sentinel_wiring

    started: list[str] = []
    monkeypatch.setattr("threading.Thread", lambda **kw: type("T", (), {"start": lambda _s: started.append("x")})())
    monkeypatch.delenv("AUTOPOIESIS_SENTINEL", raising=False)
    sentinel_wiring.start_background()
    assert started == []
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL", "1")
    sentinel_wiring.start_background()
    assert started == ["x"]


def test_every_command_reaches_the_timeline_as_it_runs():
    """A verdict with no transcript cannot be checked by the person reading it."""
    from core.remediate.sentinel import timeline

    sentinel, calls = _sentinel([_detection()])
    sentinel.poll_once()
    sentinel.poll_once()
    assert calls["logged"] == ["preflight", "execute"]
    lines = [e for e in timeline(200) if e["kind"] == "command"]
    assert [e["argv"][1] for e in lines[-2:]] == ["show", "restart"]
    assert all(e["subject"] == "demo.service" for e in lines[-2:])
