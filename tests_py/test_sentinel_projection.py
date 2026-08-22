"""The join between the sentinel's timeline and the live-situation card list.

These exist because the gap they cover was invisible for a long time: the
sentinel found a fault, fixed it and closed the chain while the page the
operator watches showed nothing at all. Both subsystems were individually
correct; nothing tested that one reached the other.
"""
from __future__ import annotations

import json

import pytest

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


def test_report_only_chain_is_not_dressed_up_as_a_fix(tmp_path, monkeypatch):
    _write(tmp_path, REPORTED, monkeypatch)
    card = sentinel_cards("zh", now=NOW + 200)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "reported"
    assert card["runbookDraft"]["planStatus"] == "blocked"
    # the gate held it, so the card must say a person is required
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is True
    assert card["reviewVerdict"]["checks"]["overreachRisk"]["status"] == "gated"
    assert any("没有可自动执行的动作" in a for a in card["runbookDraft"]["actions"])


def test_an_in_flight_chain_reads_as_in_flight(tmp_path, monkeypatch):
    _write(tmp_path, HEALED[:6], monkeypatch)   # up to preflight, nothing closed
    card = sentinel_cards("zh", now=NOW + 60)[0]
    assert card["reviewVerdict"]["verdictStatus"] == "in_flight"
    assert card["reviewVerdict"]["checks"]["overreachRisk"]["status"] == "running"
    assert card["runbookDraft"]["planStatus"] == "in_flight"
    # an unattended action inside the allowlist is not gated — the card must not
    # inherit the NetOps pipeline's blanket "approval required"
    assert card["runbookDraft"]["approvalBoundary"]["approvalRequired"] is False


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
def test_both_languages_render_every_step(tmp_path, monkeypatch, lang):
    _write(tmp_path, HEALED, monkeypatch)
    card = sentinel_cards(lang, now=NOW + 200)[0]
    assert all(step["label"] for step in card["timeline"])
    assert all(stage["label"] for stage in card["stageTelemetry"])
