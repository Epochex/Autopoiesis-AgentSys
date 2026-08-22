"""The follow-up loop is only worth having if it fails in the right ways.

Each test here pins one property that a naive implementation gets wrong:
trusting the action's return value, judging against an absolute bar instead of
the baseline, reacting to a single bad scrape, or reporting a revert it never
confirmed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.remediate import BakeIn, FollowUp, HealthProbe


def _clock():
    return datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _follow_up(events: list[tuple[str, dict]] | None = None, **bake_in) -> FollowUp:
    """A FollowUp whose window costs no real time."""
    settings = {"window_seconds": 60.0, "interval_seconds": 15.0, "grace_seconds": 0.0}
    settings.update(bake_in)
    return FollowUp(
        bake_in=BakeIn(**settings),
        emit=(lambda kind, payload: events.append((kind, payload))) if events is not None else None,
        sleep=lambda _seconds: None,
        now=_clock,
    )


def _probe(name: str, readings: list[bool]) -> HealthProbe:
    """A probe that returns the given sequence, then repeats its last value."""
    state = {"i": 0}

    def read() -> dict:
        index = min(state["i"], len(readings) - 1)
        state["i"] += 1
        return {"up": readings[index]}

    return HealthProbe(name=name, read=read, healthy=lambda reading: bool(reading["up"]))


def test_clean_window_passes_and_never_reverts():
    reverted = []
    verdict = _follow_up().run(
        "bounce_eno1",
        probes=[_probe("carrier", [True] * 12)],
        commit=lambda: True,
        revert=lambda: reverted.append(1),
    )
    assert verdict.outcome == "passed"
    assert verdict.committed is True
    assert reverted == []
    assert verdict.samples, "a window that took no readings proves nothing"


def test_regression_after_the_change_triggers_a_verified_revert():
    # Healthy at baseline, healthy once, then fails and stays failed. The
    # revert restores it, so the read-back agrees with baseline.
    readings = [True, True, False, False, True]
    reverted = []
    verdict = _follow_up().run(
        "bounce_eno1",
        probes=[_probe("carrier", readings)],
        commit=lambda: True,
        revert=lambda: reverted.append(1),
    )
    assert verdict.outcome == "reverted"
    assert verdict.regressed_probes == ["carrier"]
    assert reverted == [1], "a detected regression must actually call revert"
    assert verdict.needs_human is False


def test_probe_already_broken_before_the_change_is_not_blamed_on_it():
    # Unhealthy at baseline and still unhealthy: nothing regressed, because the
    # change did not cause it. Judging against an absolute bar would revert a
    # perfectly good fix here.
    reverted = []
    verdict = _follow_up().run(
        "restart_collector",
        probes=[_probe("events_flowing", [False] * 12)],
        commit=lambda: True,
        revert=lambda: reverted.append(1),
    )
    assert verdict.outcome == "passed"
    assert reverted == []
    assert verdict.baseline == {"events_flowing": False}


def test_single_bad_sample_does_not_trip_the_revert():
    # One bad reading between good ones is a scrape landing mid-restart.
    reverted = []
    verdict = _follow_up().run(
        "restart_collector",
        probes=[_probe("events_flowing", [True, True, False, True, True, True])],
        commit=lambda: True,
        revert=lambda: reverted.append(1),
    )
    assert verdict.outcome == "passed"
    assert reverted == []


def test_revert_that_does_not_restore_baseline_is_reported_unverified():
    # The revert runs without raising but the system still reads worse than
    # baseline. Reporting success here would be a lie.
    verdict = _follow_up().run(
        "bounce_eno1",
        probes=[_probe("carrier", [True, False, False, False, False, False])],
        commit=lambda: True,
        revert=lambda: None,  # claims success, changes nothing
    )
    assert verdict.outcome == "revert_unverified"
    assert verdict.needs_human is True
    assert "still reads worse than baseline" in verdict.detail


def test_revert_that_raises_is_reported_not_swallowed():
    def explode() -> None:
        raise RuntimeError("ssh unreachable")

    verdict = _follow_up().run(
        "bounce_eno1",
        probes=[_probe("carrier", [True, False, False, False])],
        commit=lambda: True,
        revert=explode,
    )
    assert verdict.outcome == "revert_unverified"
    assert "ssh unreachable" in verdict.detail
    assert verdict.needs_human is True


def test_regression_with_no_inverse_escalates_instead_of_passing():
    verdict = _follow_up().run(
        "prune_logs",
        probes=[_probe("disk_ok", [True, False, False, False])],
        commit=lambda: True,
        revert=None,
    )
    assert verdict.outcome == "revert_unverified"
    assert "no inverse" in verdict.detail


def test_failed_commit_opens_no_window():
    verdict = _follow_up().run(
        "bounce_eno1",
        probes=[_probe("carrier", [True] * 4)],
        commit=lambda: False,
        revert=lambda: None,
    )
    assert verdict.outcome == "not_committed"
    assert verdict.committed is False
    assert verdict.samples == [], "nothing landed, so there is nothing to watch"


def test_every_reading_reaches_the_trace():
    events: list[tuple[str, dict]] = []
    _follow_up(events, window_seconds=30.0).run(
        "bounce_eno1",
        probes=[_probe("carrier", [True] * 8)],
        commit=lambda: True,
        revert=lambda: None,
    )
    kinds = [kind for kind, _ in events]
    assert "remediation_committed" in kinds
    assert "bakein_opened" in kinds
    assert kinds.count("bakein_sampled") == sum(1 for kind, _ in events if kind == "bakein_sampled")
    assert kinds.count("bakein_sampled") >= 2
    assert "bakein_passed" in kinds


def test_revert_path_is_fully_traced():
    events: list[tuple[str, dict]] = []
    _follow_up(events).run(
        "bounce_eno1",
        probes=[_probe("carrier", [True, False, False, True])],
        commit=lambda: True,
        revert=lambda: None,
    )
    kinds = [kind for kind, _ in events]
    assert "bakein_regressed" in kinds
    assert "remediation_reverted" in kinds


def test_probe_is_re_read_every_sample():
    """A memoised probe would sample once and pass forever."""
    calls = {"n": 0}

    def read() -> dict:
        calls["n"] += 1
        return {"up": True}

    probe = HealthProbe(name="carrier", read=read, healthy=lambda r: r["up"])
    _follow_up(window_seconds=45.0, interval_seconds=15.0).run(
        "bounce_eno1", probes=[probe], commit=lambda: True
    )
    # one baseline read plus one per window tick
    assert calls["n"] >= 4


def test_window_with_no_probes_is_rejected():
    with pytest.raises(ValueError, match="proves nothing"):
        _follow_up().run("bounce_eno1", probes=[], commit=lambda: True)


def test_zero_interval_is_rejected():
    with pytest.raises(ValueError, match="interval_seconds"):
        BakeIn(interval_seconds=0)
