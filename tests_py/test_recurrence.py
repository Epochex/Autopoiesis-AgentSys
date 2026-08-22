"""The recurrence projection — the count that decides when to stop.

This number appears on screen next to a refusal to act, so it has to be
reproducible from the log by anyone who doubts it. These tests pin the counting
rule, the window, and two ways the count was quietly wrong before review caught
them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.remediate import recurrence

NOW = 1_800_000_000.0


def at(offset: float) -> str:
    return (datetime.fromtimestamp(NOW, tz=timezone.utc) + timedelta(seconds=offset)).isoformat()


def detected(offset, subject="demo.service", detector="failed_units", action="restart_unit"):
    return {"kind": "detected", "at": at(offset), "subject": subject,
            "detector": detector, "action": action, "severity": "high"}


def resolved(offset, subject="demo.service", detector="failed_units",
             action="restart_unit", samples=12):
    return {"kind": "resolved", "at": at(offset), "subject": subject,
            "detector": detector, "action": action, "outcome": "passed", "samples": samples}


def cycle(offset):
    """Loop bookkeeping — the noise the window has to see past."""
    return {"kind": "cycle", "at": at(offset), "detections": 0, "acted": 0}


def _one(events, *, now=NOW + 10_000, window=recurrence.WINDOW_SEC):
    projected = recurrence.project(events, now=now, window_sec=window)
    return projected.get("failed_units:demo.service:restart_unit") or recurrence.History(key="x")


def test_a_fix_that_is_still_holding_is_not_a_recurrence():
    """Success is not evidence that anything is wrong."""
    assert _one([detected(0), resolved(100)]).recurrences == 0


def test_a_fix_that_did_not_hold_counts_once():
    assert _one([detected(0), resolved(100), detected(200)]).recurrences == 1


def test_each_round_trip_counts_once_and_they_accumulate():
    events = []
    for i in range(4):
        events += [detected(i * 1000), resolved(i * 1000 + 100)]
    events.append(detected(5000))
    hist = _one(events)
    assert hist.recurrences == 4
    assert all(c.outcome == "passed" for c in hist.cycles)
    assert [c.samples for c in hist.cycles] == [12, 12, 12, 12]


def test_a_repair_that_never_took_hold_is_a_different_signal():
    """No `resolved` means the fix did not land — not that it landed and broke."""
    assert _one([detected(0), detected(50), detected(100)]).recurrences == 0


def test_the_window_drops_old_cycles():
    events = [detected(0), resolved(100), detected(200), resolved(300), detected(400)]
    # Both cycles are inside a wide window...
    assert _one(events, now=NOW + 500, window=10_000).recurrences == 2
    # ...and both fall out of a narrow one anchored much later.
    assert _one(events, now=NOW + 100_000, window=1_000).recurrences == 0


def test_the_key_separates_actions_on_the_same_target():
    """"Restarting X keeps not sticking" is not "X keeps having problems"."""
    events = [
        detected(0, action="restart_unit"), resolved(100, action="restart_unit"),
        detected(200, action="restart_unit"),
        detected(300, action="bounce_interface"), resolved(400, action="bounce_interface"),
        detected(500, action="bounce_interface"),
    ]
    projected = recurrence.project(events, now=NOW + 1000, window_sec=10_000)
    assert projected["failed_units:demo.service:restart_unit"].recurrences == 1
    assert projected["failed_units:demo.service:bounce_interface"].recurrences == 1


def test_the_key_separates_detectors():
    events = [
        detected(0, detector="failed_units"), resolved(100, detector="failed_units"),
        detected(200, detector="failed_units"),
        detected(10, detector="dead_interfaces"), resolved(110, detector="dead_interfaces"),
    ]
    projected = recurrence.project(events, now=NOW + 1000, window_sec=10_000)
    assert projected["failed_units:demo.service:restart_unit"].recurrences == 1
    assert "dead_interfaces:demo.service:restart_unit" not in projected


def test_events_without_a_subject_or_detector_are_ignored():
    """Loop bookkeeping has neither and must never become a cycle."""
    assert recurrence.project([cycle(0), cycle(15), {"kind": "sentinel_started", "at": at(1)}],
                              now=NOW + 100, window_sec=10_000) == {}


def test_projection_is_pure():
    """No clock, no disk, no environment — so the number can be re-derived."""
    events = [detected(0), resolved(100), detected(200)]
    first = recurrence.project(events, now=NOW + 500, window_sec=10_000)
    second = recurrence.project(events, now=NOW + 500, window_sec=10_000)
    assert first == second


# ── the ladder ───────────────────────────────────────────────────────────────

def test_the_wait_doubles_with_each_failed_repair():
    base = 600.0
    waits = [recurrence.cooldown_for(n, base) for n in range(4)]
    assert waits == [600.0, 1200.0, 2400.0, 4800.0]
    assert all(b > a for a, b in zip(waits, waits[1:]))


def test_the_wait_is_capped():
    assert recurrence.cooldown_for(20, 600.0, cap_sec=3600.0) == 3600.0


def test_escalation_fires_at_the_limit_and_not_before():
    assert not recurrence.should_escalate(2, limit=3)
    assert recurrence.should_escalate(3, limit=3)
    assert recurrence.should_escalate(9, limit=3)


# ── two ways the count was quietly wrong, caught in review ───────────────────

def test_the_window_is_bounded_by_time_not_by_line_count(tmp_path, monkeypatch):
    """A line-count tail silently shrank the 24-hour window to about eleven.

    The loop appends a `cycle` line every poll — 5,760 a day at 15s — so any
    fixed line limit truncates the window it claims to honour. A count that
    cannot be re-derived from the log is worse than no count.
    """
    path = tmp_path / "timeline.jsonl"
    events = [detected(-80_000), resolved(-79_000), detected(-78_000)]   # ~22h ago
    events += [cycle(-70_000 + i * 15) for i in range(6000)]             # a day of noise
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(path))
    hist = recurrence.history_for("failed_units", "demo.service", "restart_unit", now=NOW)
    assert hist.recurrences == 1, "the cycle at -80,000s is inside 24h and must still count"


@pytest.mark.parametrize(
    ("window", "expected"),
    [(86400, "24 小时"), (3600, "1 小时"), (5400, "1.5 小时"),
     (1800, "30 分钟"), (120, "2 分钟"), (45, "45 秒")],
)
def test_the_window_is_printed_in_a_unit_that_is_true(window, expected):
    """`int(window//3600) or 1` printed "1 小时" for every sub-hour window.

    That number goes on screen beside a refusal to act. Rounding it into
    something the log will not support is the exact failure this whole mechanism
    exists to prevent.
    """
    assert recurrence.window_label(window) == expected


def test_the_note_names_the_real_window_and_count():
    hist = recurrence.History(key="k", cycles=(recurrence.Cycle(at(0), "passed", 12),) * 3)
    note = recurrence.escalation_note(hist, window_sec=1800)
    assert "30 分钟" in note and "3 次" in note


def test_a_missing_timeline_reads_as_no_history(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(tmp_path / "absent.jsonl"))
    assert recurrence.history_for("d", "s", "a", now=NOW).recurrences == 0


def test_a_torn_last_line_does_not_break_the_count(tmp_path, monkeypatch):
    path = tmp_path / "timeline.jsonl"
    body = "\n".join(json.dumps(e) for e in [detected(-100), resolved(-50), detected(-10)])
    path.write_text(body + "\n" + '{"kind": "detected", "subj', encoding="utf-8")
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(path))
    assert recurrence.history_for("failed_units", "demo.service", "restart_unit",
                                  now=NOW).recurrences == 1
