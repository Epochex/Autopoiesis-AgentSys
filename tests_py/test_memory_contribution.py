"""设备画像只能重排调查，消融报告必须保留失败和统计边界。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.eval.memory_contribution import (
    Fact,
    build_device_questions,
    evaluate_question,
    paired_randomization_p,
    render_report,
    run_contribution,
)
from frontend.gateway.app import investigate


UTC = timezone.utc


class _Execution:
    def __init__(self, command: str):
        self.command = command

    def as_evidence(self, evidence_id: str) -> dict:
        return {
            "evidence_id": evidence_id,
            "command": self.command,
            "output": "ok\n",
            "ok": True,
            "exit_code": 0,
        }


def _fact(
    at: datetime,
    device: str,
    *,
    peer: str = "8.8.8.8",
    action: str = "accept",
    interface: str = "port5",
) -> Fact:
    return Fact(
        at=at,
        device=device,
        dst_ip=peer,
        dst_port=53,
        proto=17,
        action=action,
        event_type="traffic",
        subtype="forward",
        src_intf=interface,
        dst_intf="wan1",
        sent_bytes=10,
        rcvd_bytes=20,
    )


def _real_shaped_facts(device_count: int = 8) -> list[Fact]:
    start = datetime(2026, 8, 20, 10, tzinfo=UTC)
    rows = [
        _fact(start, "192.168.16.28"),
        _fact(start + timedelta(minutes=1), "192.168.16.28"),
        _fact(
            start + timedelta(minutes=2),
            "192.168.16.56",
            peer="255.255.255.255",
            action="deny",
            interface="LACP",
        ),
        _fact(start, "192.168.16.73", peer="1.1.1.1"),
        _fact(start + timedelta(minutes=3), "192.168.16.73", peer="1.1.1.1"),
    ]
    for index in range(max(0, device_count - 3)):
        device = f"192.168.20.{index + 1}"
        rows.extend((
            _fact(start, device),
            _fact(start + timedelta(minutes=4 + index), device),
        ))
    return rows


def test_no_portrait_keeps_the_original_order_byte_for_byte() -> None:
    original = list(investigate.TRIAGE_PROBES)
    assert investigate.order_triage_by_profile(original, []) == original
    assert original == investigate.TRIAGE_PROBES


def test_portrait_reorders_only_and_preserves_every_probe_once() -> None:
    ordered = investigate.order_triage_by_profile(
        list(investigate.TRIAGE_PROBES),
        ["first_deny", "new_peer", "new_interface"],
    )
    assert ordered[:5] == [
        "journalctl -p err -n 40 --no-pager --since -24h",
        "ip route show",
        "ip neigh show",
        "ss -tulpn",
        "ip -br link show",
    ]
    assert len(ordered) == len(investigate.TRIAGE_PROBES)
    assert set(ordered) == set(investigate.TRIAGE_PROBES)


def test_investigate_without_portrait_runs_the_exact_old_sequence(monkeypatch) -> None:
    called: list[str] = []

    def execute(command: str) -> _Execution:
        called.append(command)
        return _Execution(command)

    monkeypatch.setattr(investigate, "run", execute)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda subject: [])

    opened = investigate.start("查这台设备怎么了", subject="192.168.16.28")
    expected = [
        *investigate.BASELINE_PROBES,
        *investigate.TRIAGE_PROBES,
        "ping -c 2 -W 2 192.168.16.28",
        "ip neigh show 192.168.16.28",
    ]
    assert called == expected
    assert opened["probe_candidates"] == expected
    assert "profile_anomalies" not in opened["probe_prior"]
    assert "profile_preferred" not in opened["probe_prior"]


def test_investigate_portrait_changes_order_without_early_stop(monkeypatch) -> None:
    called: list[str] = []

    def execute(command: str) -> _Execution:
        called.append(command)
        return _Execution(command)

    monkeypatch.setattr(investigate, "run", execute)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    monkeypatch.setattr(
        investigate,
        "_device_profile_anomaly_types",
        lambda subject: ["first_deny", "new_peer"],
    )

    opened = investigate.start("查这台设备怎么了", subject="192.168.16.56")
    triage = opened["probe_candidates"][len(investigate.BASELINE_PROBES):-2]
    assert triage[:4] == [
        "journalctl -p err -n 40 --no-pager --since -24h",
        "ip route show",
        "ip neigh show",
        "ss -tulpn",
    ]
    assert set(triage) == set(investigate.TRIAGE_PROBES)
    assert len(called) == len(investigate.BASELINE_PROBES) + len(investigate.TRIAGE_PROBES) + 2
    receipts = [
        event for event in opened["trace_events"]
        if event["kind"] == "memory_candidates_ranked"
    ]
    assert len(receipts) == 1
    assert receipts[0]["payload"]["returned_count"] == 0


def test_investigate_rebuilds_the_latest_profile_decision_from_facts(monkeypatch) -> None:
    from frontend.gateway.app import history

    start = datetime(2026, 8, 20, 10, tzinfo=UTC)
    rows = [
        {
            "event_ts": (start + timedelta(minutes=1)).isoformat(),
            "srcip": "192.168.16.56",
            "dstip": "255.255.255.255",
            "dstport": 48689,
            "proto": "17",
            "action": "deny",
            "type": "traffic",
            "subtype": "forward",
            "srcintf": "LACP",
            "dstintf": "unknown0",
            "sentbyte": 0,
            "rcvdbyte": 0,
        },
        {
            "event_ts": start.isoformat(),
            "srcip": "192.168.16.56",
            "dstip": "8.8.8.8",
            "dstport": 53,
            "proto": "17",
            "action": "accept",
            "type": "traffic",
            "subtype": "forward",
            "srcintf": "port5",
            "dstintf": "wan1",
            "sentbyte": 10,
            "rcvdbyte": 20,
        },
    ]
    monkeypatch.setattr(history, "_q", lambda sql: rows)

    assert investigate._device_profile_anomaly_types("192.168.16.56") == [
        "new_peer",
        "first_deny",
        "new_interface",
    ]


def test_real_device_questions_keep_raw_profile_numbers() -> None:
    cases = build_device_questions(_real_shaped_facts())
    anomalous = next(case for case in cases if case.subject == "192.168.16.56")
    assert anomalous.baseline_device == "192.168.16.56"
    assert anomalous.anomaly_types == ("new_peer", "first_deny", "new_interface")
    assert "255.255.255.255" in anomalous.anomaly_explanations[0]
    assert "LACP" in anomalous.anomaly_explanations[2]
    assert anomalous.anomaly_numbers == (
        {"previous_sessions": 0, "current_sessions": 1},
        {"previous_denied": 0, "current_denied": 1},
        {"previous_occurrences": 0, "current_occurrences": 1},
    )


def test_four_arms_share_conclusion_and_a2_matches_hint_count() -> None:
    case = next(
        case
        for case in build_device_questions(_real_shaped_facts())
        if case.subject == "192.168.16.56"
    )
    rows = {row.arm: row for row in evaluate_question(case)}
    assert rows["M"].hint_count == rows["A2"].hint_count == 3
    assert rows["M"].conclusion == rows["A1"].conclusion == rows["A2"].conclusion
    assert rows["M"].probes_required < rows["A2"].probes_required
    assert rows["A0"].reached_same_conclusion is False
    assert rows["A0"].probes_required is None


def test_report_starts_with_sample_and_power_and_reports_all_arms() -> None:
    report = run_contribution(
        _real_shaped_facts(), source="test real fact rows", limit=8
    )
    text = render_report(report)
    assert text.splitlines()[0].startswith("样本量与功效：n=")
    assert "80% 功效" in text.splitlines()[0]
    assert "M vs A2" in text
    assert all(f"  {arm}:" in text for arm in ("M", "A1", "A2", "A0"))
    assert report.design["llm_calls"] == 0
    assert report.arms["A0"]["reached_same_conclusion_n"] == 0
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_report_uses_the_honest_no_gain_sentence_when_not_significant() -> None:
    report = run_contribution(
        _real_shaped_facts(device_count=3), source="small real cohort", limit=3
    )
    assert report.primary_comparison["significant_at_0.05"] is False
    assert "画像在这个场景没带来可测增益" in report.conclusion


def test_offline_harness_never_calls_an_llm(monkeypatch) -> None:
    from core.llm.provider import OpenAICompatibleClient

    monkeypatch.setattr(
        OpenAICompatibleClient,
        "complete_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("memory contribution attempted an LLM call")
        ),
    )
    assert run_contribution(
        _real_shaped_facts(), source="test real fact rows", limit=5
    ).design["llm_calls"] == 0


def test_paired_randomization_keeps_small_sample_boundary() -> None:
    assert paired_randomization_p([0, 0, 0]) == 1.0
    assert paired_randomization_p([4]) == 1.0
    assert paired_randomization_p([1] * 6) == pytest.approx(0.03125)
