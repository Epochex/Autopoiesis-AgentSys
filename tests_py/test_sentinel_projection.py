"""The join between the sentinel's timeline and the live-situation card list.

These exist because the gap they cover was invisible for a long time: the
sentinel found a fault, fixed it and closed the chain while the page the
operator watches showed nothing at all. Both subsystems were individually
correct; nothing tested that one reached the other.
"""
from __future__ import annotations

import json

import pytest

from frontend.gateway.app import sentinel_projection
from frontend.gateway.app.sentinel_projection import merge_into_snapshot, sentinel_cards

NOW = 1_800_000_000.0


def _at(offset: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.fromtimestamp(NOW, tz=timezone.utc) + timedelta(seconds=offset)).isoformat()


def _write(tmp_path, events, monkeypatch):
    path = tmp_path / "sentinel-timeline.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(path))
    return path


HEALED = [
    {"kind": "sentinel_started", "at": _at(0)},
    {"kind": "cycle", "at": _at(1), "detections": 0},
    {"kind": "detected", "at": _at(10), "subject": "demo.service", "severity": "high",
     "detector": "failed_units", "action": "restart_unit", "family": "fam-perception-selfheal",
     "summary": "demo.service 挂了。", "evidence": {"line": "demo.service loaded failed"}, "streak": 1},
    {"kind": "awaiting_confirmation", "at": _at(11), "subject": "demo.service", "need": 2, "streak": 1},
    {"kind": "detected", "at": _at(25), "subject": "demo.service", "severity": "high",
     "detector": "failed_units", "action": "restart_unit", "summary": "demo.service 挂了。", "streak": 2},
    {"kind": "preflight", "at": _at(26), "subject": "demo.service", "action": "restart_unit",
     "eligible": True, "reason": "unit is failed",
     "blast_radius": {"scope": "single-service", "summary": "只影响 demo.service。"}},
    {"kind": "remediated", "at": _at(120), "subject": "demo.service", "action": "restart_unit",
     "outcome": "passed", "needs_human": False, "samples": 12, "detail": "no probe regressed"},
    {"kind": "resolved", "at": _at(121), "subject": "demo.service", "note": "回读通过"},
]

REPORTED = [
    {"kind": "detected", "at": _at(30), "subject": "203.0.113.77", "severity": "high",
     "detector": "admin_bruteforce", "action": None, "summary": "失败登录 12 次。",
     "evidence": {"failures": 12}},
    {"kind": "no_safe_action", "at": _at(31), "subject": "203.0.113.77",
     "reason": "这一族没有可自动执行的动作"},
]

# The rounds that caused the refusal — each one a restart that worked and a
# fault that came back anyway.
PRIOR_CYCLES = [
    {"at": _at(20), "outcome": "passed", "samples": 12},
    {"at": _at(40), "outcome": "passed", "samples": 12},
    {"at": _at(60), "outcome": "passed", "samples": 12},
]

ESCALATION_REASON = (
    "同一处置在 24 小时内已经生效过 3 次又复发。重启治不好它——反复被弄坏说明另有原因，转人工。"
)

# One full successful cycle still in the window, then the detection the system
# refused to act on. The successes are the point: they are what the escalation
# is about, and the card must not read them as "this is fine".
RECURRING = [
    {"kind": "detected", "at": _at(15), "subject": "demo-collector.service", "severity": "high",
     "detector": "failed_units", "action": "restart_unit", "family": "fam-perception-selfheal",
     "summary": "demo-collector.service 挂了。",
     "evidence": {"line": "demo-collector.service loaded failed"}, "streak": 2},
    {"kind": "preflight", "at": _at(16), "subject": "demo-collector.service", "action": "restart_unit",
     "eligible": True, "reason": "unit is failed",
     "blast_radius": {"scope": "single-service", "summary": "只影响 demo-collector.service。"}},
    {"kind": "remediated", "at": _at(19), "subject": "demo-collector.service", "action": "restart_unit",
     "outcome": "passed", "needs_human": False, "samples": 12, "detail": "no probe regressed"},
    {"kind": "resolved", "at": _at(20), "subject": "demo-collector.service", "note": "回读通过"},
    {"kind": "detected", "at": _at(70), "subject": "demo-collector.service", "severity": "high",
     "detector": "failed_units", "action": "restart_unit",
     "summary": "demo-collector.service 又挂了。", "streak": 2},
    {"kind": "escalated", "at": _at(72), "subject": "demo-collector.service",
     "detector": "failed_units", "action": "restart_unit",
     "recurrences": 3, "window_hours": 24, "prior_cycles": PRIOR_CYCLES,
     "reason": ESCALATION_REASON},
]


def test_a_healed_chain_becomes_one_closed_card(tmp_path, monkeypatch):
    _write(tmp_path, HEALED, monkeypatch)
    cards = sentinel_cards("zh", now=NOW + 200)
    assert len(cards) == 1
    card = cards[0]
    assert card["deviceKey"] == "demo.service"
    assert card["scope"] == "sentinel"
    assert card["reviewVerdict"]["verdictStatus"] == "closed"
    assert card["priority"] == "P3"
    # every recorded step survives into the card the operator clicks
    assert [s["kind"] for s in card["timeline"]] == [e["kind"] for e in HEALED[2:]]
    assert card["runbookDraft"]["planStatus"] == "executed"


def test_live_action_and_observation_events_advance_the_card(tmp_path, monkeypatch):
    observing = HEALED[2:6] + [
        {"kind": "remediation_committed", "at": _at(27),
         "subject": "demo.service", "action": "restart_unit"},
        {"kind": "bakein_opened", "at": _at(28),
         "subject": "demo.service", "action": "restart_unit"},
        {"kind": "bakein_sampled", "at": _at(29),
         "subject": "demo.service", "action": "restart_unit", "phase": "fast"},
        {"kind": "bakein_sampled", "at": _at(44).replace("+00:00", "Z"),
         "subject": "demo.service", "action": "restart_unit", "phase": "stability"},
    ]
    _write(tmp_path, observing, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 60)[0]
    assert card["ts"] == _at(44).replace("+00:00", "Z")
    assert card["reviewVerdict"]["verdictStatus"] == "in_flight"
    assert card["reviewVerdict"]["recommendedDisposition"] == "observing"
    assert [row["kind"] for row in card["timeline"]][-4:] == [
        "remediation_committed", "bakein_opened", "bakein_sampled", "bakein_sampled",
    ]
    by_stage = {row["stageId"]: row for row in card["stageTelemetry"]}
    assert "动作回执已记录" in by_stage["act"]["detail"]
    assert by_stage["watch"]["ts"] == _at(44).replace("+00:00", "Z")
    assert by_stage["watch"]["detail"] == "稳定性窗口 · 已完成 2 次健康回读"


def test_report_only_chain_is_not_dressed_up_as_a_fix(tmp_path, monkeypatch):
    _write(tmp_path, REPORTED, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "reported"
    assert card["runbookDraft"]["planStatus"] == "blocked"
    # the gate held it, so the card must say a person is required
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is True
    assert card["reviewVerdict"]["checks"]["overreachRisk"]["status"] == "gated"
    assert any("自动执行条件未满足" in a for a in card["runbookDraft"]["actions"])
    assert any("候选动作：临时防火墙封禁（未执行）" in a
               for a in card["runbookDraft"]["actions"])
    reason = next(stage for stage in card["stageTelemetry"] if stage["stageId"] == "gate")["detail"]
    assert "归属确认" in reason
    assert "管理地址豁免" in reason
    assert "封禁 TTL" in reason
    assert "超时自动回滚" in reason


def test_escalation_outranks_the_successes_that_caused_it(tmp_path, monkeypatch):
    """The same chain, with and without the refusal on the end.

    A recurring subject's chain still holds every `remediated` and `resolved`
    from the rounds that did work. Reading the last of those would report the
    service as healed at the exact moment the system refused to touch it again.
    """
    _write(tmp_path, RECURRING[:4], monkeypatch)
    assert sentinel_cards("zh", now=NOW + 200)[0]["reviewVerdict"]["verdictStatus"] == "closed"

    _write(tmp_path, RECURRING, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "escalated"
    assert card["reviewVerdict"]["recommendedDisposition"] == "needs_human"
    assert card["priority"] == "P1"
    assert card["runbookDraft"]["planStatus"] == "blocked"
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is True
    assert card["reviewVerdict"]["checks"]["overreachRisk"]["status"] == "gated"


def test_the_citation_chain_reaches_the_card(tmp_path, monkeypatch):
    """"凭什么第三次就不修了" has to be answerable from the card, not the docs."""
    _write(tmp_path, RECURRING, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["recurrences"] == 3
    assert card["priorCycles"] == PRIOR_CYCLES
    # and the runbook stops claiming it will restart the unit
    assert ESCALATION_REASON in card["runbookDraft"]["actions"]


def test_the_escalation_is_a_stage_of_its_own(tmp_path, monkeypatch):
    _write(tmp_path, RECURRING, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    stage = next(s for s in card["stageTelemetry"] if s["stageId"] == "escalated")
    # an aggregation over the timeline, not a detector and not a model
    assert stage["provider"] == "recurrence"
    assert stage["detail"] == ESCALATION_REASON
    assert card["timeline"][-1]["kind"] == "escalated"
    # deliberately not the same words as `needs_human`: that one means a revert
    # could not be verified, this one means the system chose to stop repairing
    assert card["timeline"][-1]["label"] == "不再自动修，转人工"


def test_the_refusal_sticks_while_the_detector_keeps_firing(tmp_path, monkeypatch):
    """The sentinel announces an escalation once per key, then keeps detecting.

    Read from the tail, such a chain is a run of bare `detected` events with the
    decision scrolled off the end. Nothing may read that as a fresh incident the
    system is about to work on.
    """
    still_broken = [
        {"kind": "detected", "at": _at(90 + 20 * i), "subject": "demo-collector.service",
         "severity": "high", "detector": "failed_units", "action": "restart_unit",
         "summary": "demo-collector.service 又挂了。", "streak": 2}
        for i in range(3)
    ]
    _write(tmp_path, RECURRING + still_broken, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 300)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "escalated"
    assert card["runbookDraft"]["planStatus"] == "blocked"
    assert card["recurrences"] == 3
    # its clock still advances, so it stays at the top of the live list
    assert card["ts"] == still_broken[-1]["at"]


def test_a_chain_that_never_escalated_still_answers_the_question(tmp_path, monkeypatch):
    """Empty, not absent — a reader must not have to know the chain's kind first."""
    _write(tmp_path, HEALED, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["recurrences"] == 0
    assert card["priorCycles"] == []


def test_an_in_flight_chain_reads_as_in_flight(tmp_path, monkeypatch):
    _write(tmp_path, HEALED[:6], monkeypatch)   # up to preflight, nothing closed
    card = sentinel_cards("zh", now=NOW + 60)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "in_flight"
    assert card["reviewVerdict"]["checks"]["overreachRisk"]["status"] == "running"
    assert card["runbookDraft"]["planStatus"] == "in_flight"
    # an unattended action inside the allowlist is not gated — the card must not
    # inherit the NetOps pipeline's blanket "approval required"
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is False


def test_a_new_detection_after_resolution_opens_a_fresh_cycle(tmp_path, monkeypatch):
    current = [
        *HEALED,
        {"kind": "detected", "at": _at(180), "subject": "demo.service", "severity": "high",
         "detector": "failed_units", "action": "restart_unit", "summary": "demo.service 又挂了。",
         "evidence": {"line": "demo.service loaded failed"}, "streak": 2},
        {"kind": "cooldown", "at": _at(181), "subject": "demo.service",
         "action": "restart_unit", "remaining_sec": 419},
    ]
    _write(tmp_path, current, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "cooling"
    assert card["runbookDraft"]["planStatus"] == "blocked"
    assert [step["kind"] for step in card["timeline"]] == ["detected", "cooldown"]
    assert card["timeline"][0]["ts"] == _at(180)
    assert any(stage["stageId"] == "cooldown" for stage in card["stageTelemetry"])


def test_stale_chains_drop_out_of_the_live_list(tmp_path, monkeypatch):
    _write(tmp_path, HEALED, monkeypatch)
    assert sentinel_cards("zh", now=NOW + 7 * 3600) == []


def test_loop_bookkeeping_never_becomes_a_card(tmp_path, monkeypatch):
    _write(tmp_path, [HEALED[0], HEALED[1]], monkeypatch)
    assert sentinel_cards("zh", now=NOW + 60) == []


def test_a_torn_last_line_does_not_break_the_page(tmp_path, monkeypatch):
    path = _write(tmp_path, HEALED, monkeypatch)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "detected", "subject": "half-writ')
    assert len(sentinel_cards("zh", now=NOW + 200)) == 1


def test_a_missing_timeline_leaves_the_snapshot_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(tmp_path / "absent.jsonl"))
    before = {"ready": False, "feed": [], "suggestions": [], "defaultSuggestionId": ""}
    assert merge_into_snapshot(dict(before), "zh") == before


def test_merge_puts_the_live_incident_in_front_of_the_corpus(tmp_path, monkeypatch):
    _write(tmp_path, HEALED, monkeypatch)
    snapshot = {
        "ready": True,
        "feed": [{"id": "feed-suggestion-old", "kind": "suggestion", "ts": "2026-07-22T13:13:26+00:00"}],
        "suggestions": [{"id": "old", "ts": "2026-07-22T13:13:26+00:00", "scope": "cluster"}],
        "defaultSuggestionId": "old",
    }
    merged = merge_into_snapshot(snapshot, "zh")
    assert merged["suggestions"][0]["scope"] == "sentinel"
    # the card the operator just caused is the one selected on arrival
    assert merged["defaultSuggestionId"] == merged["suggestions"][0]["id"]
    assert len(merged["feed"]) == 2
    assert merged["feed"][0]["deviceKey"] == "demo.service"


def test_the_card_id_is_stable_across_polls(tmp_path, monkeypatch):
    _write(tmp_path, HEALED[:4], monkeypatch)
    early = sentinel_cards("zh", now=NOW + 20)[0]["id"]
    _write(tmp_path, HEALED, monkeypatch)
    late = sentinel_cards("zh", now=NOW + 200)[0]["id"]
    # a changing id would re-mount the detail pane mid-incident and lose the
    # operator's place every time the chain advanced
    assert early == late


@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("events", [HEALED, RECURRING], ids=["healed", "escalated"])
def test_both_languages_render_every_step(tmp_path, monkeypatch, lang, events):
    _write(tmp_path, events, monkeypatch)
    card = sentinel_cards(lang, now=NOW + 200)[0]
    assert all(step["label"] for step in card["timeline"])
    assert all(stage["label"] for stage in card["stageTelemetry"])


def test_the_card_says_which_topology_node_it_concerns(tmp_path, monkeypatch):
    """Without this the theater drew the chain into nothing and the map stayed silent."""
    _write(tmp_path, HEALED, monkeypatch)
    monkeypatch.setattr(sentinel_projection, "_HOST_ADDRESS", ["192.168.1.27"])
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["anchorIp"] == "192.168.1.27"
    # a unit name is not an address, so there is no separate origin to name
    assert card["originIp"] is None


def test_an_address_subject_keeps_its_origin_separate_from_the_target(tmp_path, monkeypatch):
    """The source is off-network; the host under attack is still ours."""
    _write(tmp_path, REPORTED, monkeypatch)
    monkeypatch.setattr(sentinel_projection, "_HOST_ADDRESS", ["192.168.1.27"])
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["anchorIp"] == "192.168.1.27"
    assert card["originIp"] == "203.0.113.77"


def test_an_unreadable_host_address_does_not_break_the_card(tmp_path, monkeypatch):
    _write(tmp_path, HEALED, monkeypatch)
    monkeypatch.setattr(sentinel_projection, "_HOST_ADDRESS", [None])
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["anchorIp"] is None, "no anchor is honest; a guessed one is not"
    assert card["deviceKey"] == "demo.service"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("203.0.113.77", True), ("192.168.1.27", True), ("demo.service", False),
     ("eth0", False), ("999.1.1.1", False), ("1.2.3", False)],
)
def test_only_a_real_address_is_treated_as_an_origin(value, expected):
    assert sentinel_projection._is_ipv4(value) is expected


def test_the_host_address_is_the_carrier_bearing_private_one(monkeypatch):
    """`lo` and down links must never be mistaken for the host's place on the network."""
    monkeypatch.setattr(sentinel_projection, "_HOST_ADDRESS", [])
    monkeypatch.setattr(
        "core.investigate.safe_exec.run",
        lambda _cmd: type("R", (), {"output": (
            "lo    UNKNOWN  127.0.0.1/8 ::1/128\n"
            "eth0  DOWN\n"
            "eth2  UP       192.168.1.27/24\n"
        )})(),
    )
    assert sentinel_projection.host_address() == "192.168.1.27"


def test_an_escalation_ends_when_the_target_stops_failing(tmp_path, monkeypatch):
    """A person fixed it. The card has to be able to notice that.

    The loop's in-process latch is invisible to anything reading the log, so
    without an event for it the card would stay "needs a person" forever after
    the person had been.
    """
    healed = [*RECURRING, {
        "kind": "escalation_cleared", "at": _at(9_000), "subject": "demo.service",
        "detector": "failed_units", "action": "restart_unit",
        "note": "该目标已不再报错，升级状态解除；复发计数仍在窗口内保留",
    }]
    _write(tmp_path, healed, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 9_200)[0]
    assert card["reviewVerdict"]["verdictStatus"] != "escalated"
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is False


def test_a_clearance_from_an_earlier_escalation_does_not_lift_a_later_one(tmp_path, monkeypatch):
    """Escalated, cleared, escalated again — the newest decision is the one that counts."""
    events = [
        *RECURRING,
        {"kind": "escalation_cleared", "at": _at(9_000), "subject": "demo.service",
         "detector": "failed_units", "action": "restart_unit", "note": "n"},
        {"kind": "detected", "at": _at(10_000), "subject": "demo.service",
         "detector": "failed_units", "action": "restart_unit", "severity": "high",
         "summary": "demo.service 挂了。"},
        {"kind": "escalated", "at": _at(10_100), "subject": "demo.service",
         "detector": "failed_units", "action": "restart_unit", "recurrences": 3,
         "window_hours": 24, "prior_cycles": [], "reason": ESCALATION_REASON},
    ]
    _write(tmp_path, events, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 10_300)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "escalated"
