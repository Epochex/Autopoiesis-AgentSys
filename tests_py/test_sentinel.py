"""The autonomous loop, and the branches where it declines to act.

An autonomous remediator's dangerous failure is not a wrong action; it is the
same action repeated forever, or an action taken on one noisy sample. Every
test here pins one of the brakes.
"""

from __future__ import annotations

import pytest

from core.remediate.sentinel import Detection, Sentinel


def _detection(subject="demo.service", action="restart_unit"):
    return Detection(detector="d", family="f", subject=subject, severity="high",
                     summary="s", action=action, target=subject)


def _sentinel(detections, *, eligible=True, outcome="passed", clock=None):
    calls = {"preflight": [], "execute": []}

    def preflight(action, target):
        calls["preflight"].append((action, target))
        return {"eligible": eligible, "reason": "" if eligible else "preconditions not met"}

    def execute(action, target):
        calls["execute"].append((action, target))
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
                        execute=lambda a, t: {"verdict": {"outcome": "passed"}},
                        preflight=lambda a, t: {"eligible": True}, confirm_polls=2)
    sentinel.poll_once()
    findings.clear()
    sentinel.poll_once()
    findings.append(_detection())
    sentinel.poll_once()
    assert sentinel._streak["d:demo.service"] == 1, "the streak must have restarted"


def test_a_detection_with_no_safe_action_is_reported_not_improvised_on():
    sentinel, calls = _sentinel([_detection(action=None)])
    sentinel.poll_once()
    sentinel.poll_once()
    assert calls["execute"] == []
    assert calls["preflight"] == []


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


def test_a_passing_action_clears_the_cooldown():
    """A later unrelated fault on the same target must not be ignored."""
    now = {"t": 0.0}
    sentinel, calls = _sentinel([_detection()], outcome="passed", clock=lambda: now["t"])
    sentinel.poll_once(); sentinel.poll_once()
    assert len(calls["execute"]) == 1
    now["t"] += 5
    sentinel.poll_once(); sentinel.poll_once()
    assert len(calls["execute"]) == 2


def test_a_broken_detector_does_not_stop_the_others():
    def explodes():
        raise RuntimeError("detector bug")

    sentinel = Sentinel(
        detectors=[explodes, lambda: [_detection()]],
        execute=lambda a, t: {"verdict": {"outcome": "passed"}},
        preflight=lambda a, t: {"eligible": True}, confirm_polls=1,
    )
    result = sentinel.poll_once()
    assert len(result["detections"]) == 1


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
