"""Environment perception: detectors, the L2 identity source, and coverage.

Every test builds its own synthetic FortiOS corpus. The real syslog fixtures
are git-ignored, so a test that reads them would pass on this machine and fail
on a fresh checkout -- and a perception layer whose tests only run where the
data already is proves nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domains.network_rca.environment import (
    _CLOSES_WITH_ZH,
    FAULT_CLASS_REQUIREMENTS,
    _LIMIT_ZH,
    _PLAYBOOKS,
    _PLAYBOOK_ZH,
    address_space,
    arp_snapshot_provenance,
    build_environment_report,
    detect_dhcp_duplicate_ip,
    detect_host_multi_address,
    detect_identity_contradiction,
    detect_l2_ownership_drift,
    detect_lease_churn,
    detect_mgmt_bruteforce,
    detect_pool_pressure,
    detect_session_clash,
    detect_unmanaged_address,
    detect_unmanaged_identity,
    load_arp_snapshot,
    load_l2_history,
    parse_arp_table,
    sensor_coverage,
    source_registry,
    sweep_corpus,
    verify_findings,
    _is_locally_administered,
)


SERVER_MAC = "50:9a:4c:87:29:b3"
SQUATTER_MAC = "d4:43:0e:1a:c5:88"


def _ack(date: str, time: str, ip: str, mac: str, lease: int = 604800, hostname: str = "N/A") -> str:
    return (
        f'date={date} time={time} devname="FGT" type="event" subtype="system" '
        f'logdesc="DHCP Ack log" interface="fortilink" dhcp_msg="Ack" mac="{mac}" '
        f"ip={ip} lease={lease} " + f'hostname="{hostname}" msg="DHCP server sends a DHCPACK"'
    )


def _flow(date: str, time: str, srcip: str, dstip: str) -> str:
    return (
        f'date={date} time={time} devname="FGT" type="traffic" subtype="forward" '
        f'srcip={srcip} dstip={dstip} action="accept" policyid=1'
    )


def _clash(date: str, time: str, a: str, b: str) -> str:
    return (
        f'date={date} time={time} devname="FGT" type="event" subtype="system" '
        f'logdesc="session clash" status="clash" proto=17 msg="session clash" '
        f'new_status="{a}:3702->10.0.0.9:100(1.2.3.4:64118)" '
        f'old_status="{b}:3702->10.0.0.9:100(1.2.3.4:64118)"'
    )


def _admin_fail(date: str, time: str, srcip: str) -> str:
    return (
        f'date={date} time={time} devname="FGT" type="event" subtype="system" '
        f'logdesc="Admin login failed" srcip={srcip} user="admin" msg="Administrator admin login failed"'
    )


def _stats(date: str, time: str, pool: str, total: int, used: int) -> str:
    return (
        f'date={date} time={time} devname="FGT" type="event" subtype="system" '
        f'logdesc="DHCP statistics" interface="{pool}" total={total} used={used} msg="DHCP statistics"'
    )


def _corpus(tmp_path: Path, lines: list[str]) -> list[Path]:
    path = tmp_path / "fortigate-test.log"
    path.write_text("\n".join(lines) + "\n")
    return [path]


# ---------------------------------------------------------------------------
# corpus sweep
# ---------------------------------------------------------------------------

def test_sweep_declares_only_the_sources_the_corpus_actually_contains(tmp_path):
    paths = _corpus(tmp_path, [_ack("2026-06-16", "00:00:01", "192.168.1.8", SERVER_MAC)])
    observations = sweep_corpus(paths)

    assert observations["sources_seen"] == {"dhcp_ack"}
    assert observations["leased_ips"] == {"192.168.1.8"}
    assert observations["served_segments"] == {"192.168.1.0/24"}
    assert observations["corpus"]["lines_read"] == 1


def test_sweep_over_an_empty_path_list_yields_no_sources_and_no_segments():
    observations = sweep_corpus([])
    assert observations["sources_seen"] == set()
    assert observations["served_segments"] == set()


# ---------------------------------------------------------------------------
# duplicate address, both claimants on DHCP
# ---------------------------------------------------------------------------

def test_dhcp_duplicate_ip_fires_when_two_macs_lease_one_address_inside_the_window(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _ack("2026-06-16", "00:01:00", "192.168.1.23", "02:11:22:33:44:55"),
        ],
    )
    findings = detect_dhcp_duplicate_ip(sweep_corpus(paths))

    assert len(findings) == 1
    assert findings[0]["fault_class"] == "duplicate_ip_dhcp"
    assert findings[0]["severity"] == "critical"
    assert findings[0]["measured"]["mac_count"] == 2
    assert findings[0]["measured"]["closest_gap_seconds"] == 60


def test_dhcp_duplicate_ip_stays_silent_when_the_claims_are_far_apart(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _ack("2026-06-16", "06:00:00", "192.168.1.23", "02:11:22:33:44:55"),
        ],
    )
    assert detect_dhcp_duplicate_ip(sweep_corpus(paths), window_seconds=300) == []


# ---------------------------------------------------------------------------
# the .23 class: one claimant never speaks DHCP
# ---------------------------------------------------------------------------

def test_dhcp_alone_cannot_see_a_static_squatter(tmp_path):
    """The exact reason 192.168.1.23 was invisible: the squatter emits nothing."""
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _flow("2026-06-16", "00:00:05", "192.168.1.23", "192.168.30.35"),
        ],
    )
    observations = sweep_corpus(paths)

    assert detect_dhcp_duplicate_ip(observations) == []
    assert detect_identity_contradiction(observations, arp_records=[]) == []

    coverage = {row["fault_class"]: row for row in sensor_coverage(observations, [])}
    assert coverage["duplicate_ip_static"]["coverage"] == "blind"
    assert coverage["duplicate_ip_static"]["missing"] == ["l2_identity"]
    assert "get system arp" in coverage["duplicate_ip_static"]["closes_with"]


def test_l2_snapshot_turns_the_blind_class_into_a_confirmed_finding(tmp_path):
    paths = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    observations = sweep_corpus(paths)
    arp = [{"ip": "192.168.1.23", "mac": SQUATTER_MAC, "interface": "fortilink"}]

    findings = detect_identity_contradiction(observations, arp)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["fault_class"] == "duplicate_ip_static"
    assert finding["severity"] == "critical"
    assert finding["measured"]["arp_macs"] == [SQUATTER_MAC]
    assert finding["measured"]["dhcp_leased_macs"] == [SERVER_MAC]

    coverage = {row["fault_class"]: row for row in sensor_coverage(observations, arp)}
    assert coverage["duplicate_ip_static"]["coverage"] == "covered"


def test_two_macs_in_one_arp_snapshot_are_a_conflict_without_any_dhcp_record():
    findings = detect_identity_contradiction(
        sweep_corpus([]),
        [
            {"ip": "192.168.1.23", "mac": SERVER_MAC, "interface": "fortilink"},
            {"ip": "192.168.1.23", "mac": SQUATTER_MAC, "interface": "fortilink"},
        ],
    )
    assert len(findings) == 1
    assert findings[0]["measured"]["arp_macs"] == sorted([SERVER_MAC, SQUATTER_MAC])
    assert findings[0]["measured"]["dhcp_leased_macs"] == []


def test_arp_agreeing_with_the_lease_is_not_a_finding(tmp_path):
    paths = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    arp = [{"ip": "192.168.1.23", "mac": SERVER_MAC.upper(), "interface": "fortilink"}]
    assert detect_identity_contradiction(sweep_corpus(paths), arp) == []


# ---------------------------------------------------------------------------
# ARP parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Address           Age(min)   Hardware Addr      Interface\n"
        "192.168.1.23      0          D4:43:0E:1A:C5:88  fortilink\n",
        "index=6 ifname=fortilink 192.168.1.23 d4:43:0e:1a:c5:88 state=00000002\n",
        "192.168.1.23 dev eth2 lladdr d4:43:0e:1a:c5:88 REACHABLE\n",
    ],
    ids=["get-system-arp", "diagnose-ip-arp-list", "ip-neigh"],
)
def test_parse_arp_table_accepts_every_dump_an_operator_can_produce(text):
    records = parse_arp_table(text)
    assert records == [{"ip": "192.168.1.23", "mac": SQUATTER_MAC, "interface": records[0]["interface"]}]


def test_snapshot_age_travels_with_the_finding_and_stale_evidence_says_so(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    snapshot = tmp_path / "arp.txt"
    snapshot.write_text("192.168.1.23 dev eth2 lladdr d4:43:0e:1a:c5:88 REACHABLE\n")
    arp = parse_arp_table(snapshot.read_text())

    fresh = arp_snapshot_provenance(snapshot)
    assert fresh["stale"] is False
    assert fresh["captured_at"] is not None

    stale_when = datetime.fromisoformat(fresh["captured_at"].replace("Z", "+00:00")) + timedelta(hours=4)
    stale = arp_snapshot_provenance(snapshot, now=stale_when)
    assert stale["stale"] is True

    fresh_finding = detect_identity_contradiction(sweep_corpus(corpus), arp, fresh)[0]
    stale_finding = detect_identity_contradiction(sweep_corpus(corpus), arp, stale)[0]

    assert fresh_finding["measured"]["snapshot_captured_at"] == fresh["captured_at"]
    assert not any("freshness bound" in limit for limit in fresh_finding["cannot_prove"])
    assert any("freshness bound" in limit for limit in stale_finding["cannot_prove"])


def _ledger(tmp_path: Path, captures: list[tuple[str, str]]) -> Path:
    """captures = [(captured_at, mac_owning_192.168.1.23), ...]"""
    path = tmp_path / "l2.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "captured_at": stamp,
                    "records": [{"ip": "192.168.1.23", "mac": mac, "interface": "eth2"}],
                }
            )
            for stamp, mac in captures
        )
        + "\n"
    )
    return path


def test_alternating_ownership_is_only_visible_across_captures(tmp_path):
    """The .23 fault: each snapshot alone looks healthy, the sequence does not."""
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    observations = sweep_corpus(corpus)

    single = load_l2_history(
        _ledger(tmp_path, [("2026-08-05T15:59:02Z", SERVER_MAC)])
    )
    assert detect_l2_ownership_drift(observations, single) == []
    assert detect_identity_contradiction(observations, single[0]["records"]) == []

    alternating = load_l2_history(
        _ledger(
            tmp_path,
            [
                ("2026-08-05T15:29:00Z", SQUATTER_MAC),
                ("2026-08-05T15:59:02Z", SERVER_MAC),
                ("2026-08-05T16:29:00Z", SQUATTER_MAC),
            ],
        )
    )
    findings = detect_l2_ownership_drift(observations, alternating)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["fault_class"] == "duplicate_ip_static"
    assert finding["severity"] == "critical"
    assert finding["measured"]["handovers"] == 2
    assert finding["measured"]["macs"] == sorted([SERVER_MAC, SQUATTER_MAC])
    assert finding["measured"]["not_leased_by_this_server"] == [SQUATTER_MAC]


def test_a_stable_owner_across_many_captures_is_not_drift(tmp_path):
    history = load_l2_history(
        _ledger(tmp_path, [(f"2026-08-05T1{hour}:00:00Z", SERVER_MAC) for hour in range(5)])
    )
    assert detect_l2_ownership_drift(sweep_corpus([]), history) == []


def test_drifted_address_stays_contested_even_when_the_last_capture_looks_clean(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    ledger = _ledger(
        tmp_path,
        [("2026-08-05T15:29:00Z", SQUATTER_MAC), ("2026-08-05T15:59:02Z", SERVER_MAC)],
    )
    report = build_environment_report(corpus, l2_ledger_path=ledger)

    cells = {
        cell["ip"]: cell
        for segment in report["address_space"]
        for cell in segment["cells"]
    }
    assert cells["192.168.1.23"]["state"] == "contested"
    assert report["sensors"]["l2_history_captures"] == 2
    coverage = {row["fault_class"]: row for row in report["coverage"]}
    assert coverage["duplicate_ip_static"]["coverage"] == "covered"


def test_drift_and_point_in_time_contradiction_do_not_double_report_one_address(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    snapshot = tmp_path / "arp.txt"
    snapshot.write_text(f"192.168.1.23 dev eth2 lladdr {SQUATTER_MAC} REACHABLE\n")
    ledger = _ledger(
        tmp_path,
        [("2026-08-05T15:29:00Z", SQUATTER_MAC), ("2026-08-05T15:59:02Z", SERVER_MAC)],
    )
    report = build_environment_report(corpus, arp_snapshot_path=snapshot, l2_ledger_path=ledger)

    rows = [f for f in report["findings"] if f["subject"] == "192.168.1.23"]
    assert [f["detector"] for f in rows] == ["l2_ownership_drift"]


def test_a_truncated_ledger_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "l2.jsonl"
    path.write_text(
        json.dumps({"captured_at": "2026-08-05T15:00:00Z", "records": []})
        + "\n{\"captured_at\": \"2026-08-05T15:3\n"
    )
    assert len(load_l2_history(path)) == 1


# ---------------------------------------------------------------------------
# live re-verification: only a live source may retire a finding
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)


def _capture(minutes_ago: int, records: list[tuple[str, str]]) -> dict:
    stamp = _now() - timedelta(minutes=minutes_ago)
    return {
        "captured_at": stamp.isoformat().replace("+00:00", "Z"),
        "records": [{"ip": ip, "mac": mac, "interface": "eth2"} for ip, mac in records],
    }


def test_a_drift_still_happening_is_confirmed_and_kept():
    finding = {
        "subject": "192.168.1.23", "fault_class": "duplicate_ip_static",
        "severity": "critical", "confidence": 0.97, "headline": "x",
    }
    history = [
        _capture(30, [("192.168.1.23", SQUATTER_MAC)]),
        _capture(20, [("192.168.1.23", SERVER_MAC)]),
        _capture(10, [("192.168.1.23", SQUATTER_MAC)]),
    ]
    open_findings, resolved = verify_findings(
        [finding], l2_history=history, flow_store={}, now=_now()
    )
    assert resolved == []
    assert open_findings[0]["verification"]["state"] == "confirmed"
    assert "handovers" in open_findings[0]["verification"]["note"]


def test_a_drift_that_settled_back_on_the_lease_holder_is_retired():
    finding = {
        "subject": "192.168.1.23", "fault_class": "duplicate_ip_static",
        "severity": "critical", "confidence": 0.97, "headline": "x",
        "measured": {"dhcp_leased_macs": [SERVER_MAC]},
    }
    history = [_capture(minutes, [("192.168.1.23", SERVER_MAC)]) for minutes in (30, 20, 10)]
    open_findings, resolved = verify_findings(
        [finding], l2_history=history, flow_store={}, now=_now()
    )
    assert open_findings == []
    assert resolved[0]["verification"]["state"] == "resolved"
    assert SERVER_MAC in resolved[0]["verification"]["note"]


def test_settling_on_the_squatter_is_the_fault_getting_worse_not_resolved():
    """A stable owner that holds no lease means the lease holder lost the address."""
    finding = {
        "subject": "192.168.1.23", "fault_class": "duplicate_ip_static",
        "severity": "critical", "confidence": 0.97, "headline": "x",
        "measured": {"dhcp_leased_macs": [SERVER_MAC]},
    }
    history = [_capture(minutes, [("192.168.1.23", SQUATTER_MAC)]) for minutes in (30, 20, 10)]
    open_findings, resolved = verify_findings(
        [finding], l2_history=history, flow_store={}, now=_now()
    )
    assert resolved == []
    assert open_findings[0]["verification"]["state"] == "confirmed"
    assert "holds no lease" in open_findings[0]["verification"]["note"]
    assert "lost it entirely" in open_findings[0]["verification"]["note"]


def test_a_stable_owner_with_no_lease_record_at_all_is_unverifiable():
    finding = {
        "subject": "192.168.1.23", "fault_class": "duplicate_ip_static",
        "severity": "critical", "confidence": 0.97, "headline": "x",
        "measured": {"dhcp_leased_macs": []},
    }
    history = [_capture(minutes, [("192.168.1.23", SQUATTER_MAC)]) for minutes in (30, 20, 10)]
    open_findings, resolved = verify_findings(
        [finding], l2_history=history, flow_store={}, now=_now()
    )
    assert resolved == []
    assert open_findings[0]["verification"]["state"] == "unverifiable"


def test_captures_outside_the_recheck_window_do_not_count_as_live():
    """A conflict that stopped being observed hours ago is unverifiable, not fixed."""
    finding = {
        "subject": "192.168.1.23", "fault_class": "duplicate_ip_static",
        "severity": "critical", "confidence": 0.97, "headline": "x",
    }
    stale = [
        _capture(600, [("192.168.1.23", SQUATTER_MAC)]),
        _capture(590, [("192.168.1.23", SERVER_MAC)]),
    ]
    open_findings, resolved = verify_findings(
        [finding], l2_history=stale, flow_store={}, now=_now()
    )
    assert resolved == []
    assert open_findings[0]["verification"]["state"] == "unverifiable"


def test_age_alone_never_retires_a_finding():
    """The corpus ending in June is not evidence that a June fault was fixed."""
    findings = [
        {"subject": "192.168.16.0/24", "fault_class": "lease_churn", "severity": "high",
         "confidence": 0.8, "headline": "x"},
        {"subject": "management_plane", "fault_class": "mgmt_bruteforce", "severity": "critical",
         "confidence": 0.95, "headline": "y"},
    ]
    open_findings, resolved = verify_findings(
        findings, l2_history=[], flow_store={}, now=_now()
    )
    assert resolved == []
    assert [f["verification"]["state"] for f in open_findings] == ["unverifiable", "unverifiable"]
    assert all("live DHCP/event feed" in f["verification"]["note"] for f in open_findings)


def test_unmanaged_address_is_retired_only_when_the_flow_store_says_it_went_quiet():
    findings = [
        {"subject": "192.168.1.50", "fault_class": "address_unmanaged", "severity": "medium",
         "confidence": 0.85, "headline": "busy"},
        {"subject": "192.168.1.20", "fault_class": "address_unmanaged", "severity": "medium",
         "confidence": 0.85, "headline": "quiet"},
    ]
    store = {"available": True, "recent_ok": True, "flowing": True, "recent_days": 7,
             "recent_talkers": {"192.168.1.50": 122}}
    open_findings, resolved = verify_findings(
        findings, l2_history=[], flow_store=store, now=_now()
    )
    assert [f["subject"] for f in open_findings] == ["192.168.1.50"]
    assert [f["subject"] for f in resolved] == ["192.168.1.20"]


def test_an_unavailable_flow_store_cannot_retire_anything():
    findings = [{"subject": "192.168.1.20", "fault_class": "address_unmanaged",
                 "severity": "medium", "confidence": 0.85, "headline": "x"}]
    open_findings, resolved = verify_findings(
        findings, l2_history=[], flow_store={"available": False}, now=_now()
    )
    assert resolved == []
    assert open_findings[0]["verification"]["state"] == "unverifiable"


def test_a_stalled_flow_store_names_the_window_its_verdict_rests_on():
    findings = [{"subject": "192.168.1.20", "fault_class": "address_unmanaged",
                 "severity": "medium", "confidence": 0.85, "headline": "x"}]
    store = {"available": True, "recent_ok": True, "flowing": False, "recent_days": 7,
             "window_end": "2026-08-05T10:29:31Z", "recent_talkers": {}}
    _, resolved = verify_findings(findings, l2_history=[], flow_store=store, now=_now())
    assert "2026-08-05T10:29:31Z" in resolved[0]["verification"]["note"]


def test_source_registry_calls_a_stalled_pipeline_stalled(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC)])
    observations = sweep_corpus(corpus)
    history = [_capture(3, [("192.168.1.8", SERVER_MAC)])]
    store = {"available": True, "flowing": False, "rows": 61807889,
             "window_start": "2026-05-01T22:00:39Z", "window_end": "2026-08-05T10:29:31Z",
             "age_seconds": 85000.0, "recent_days": 7, "recent_talkers": {}}
    rows = {row["id"]: row for row in source_registry(
        observations,
        {"captured_at": None, "age_seconds": None, "stale": None},
        history, store, 0, now=_now(),
    )}
    assert rows["l2_identity_history"]["flowing"] is True
    assert rows["l2_identity_history"]["kind"] == "live"
    assert rows["flow_store"]["flowing"] is False
    assert "STALLED" in rows["flow_store"]["note"]
    assert rows["gateway_syslog"]["flowing"] is False
    assert rows["gateway_syslog"]["kind"] == "historical"


def test_every_open_finding_carries_a_playbook_with_a_gated_fix(tmp_path):
    corpus = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.50", "192.168.30.35"),
            _admin_fail("2026-06-16", "00:00:02", "93.152.221.5"),
        ]
        + [_admin_fail("2026-06-16", "00:00:03", "93.152.221.5") for _ in range(30)],
    )
    report = build_environment_report(corpus, flow_store={"available": False}, now=_now())
    assert report["findings"]
    for finding in report["findings"]:
        playbook = finding["playbook"]
        assert playbook["verify"], finding["finding_id"]
        assert all(step["risk"] in {"readonly", "gated"} for step in playbook["verify"])
        assert all(step["command"] for step in playbook["verify"] + playbook["fix"])


def test_report_declares_itself_online_only_when_a_source_is_flowing(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC)])
    offline = build_environment_report(corpus, flow_store={"available": False}, now=_now())
    assert offline["current_online_observation"] is False

    ledger = tmp_path / "l2.jsonl"
    ledger.write_text(json.dumps(_capture(3, [("192.168.1.8", SERVER_MAC)])) + "\n")
    online = build_environment_report(
        corpus, l2_ledger_path=ledger, flow_store={"available": False}, now=_now()
    )
    assert online["current_online_observation"] is True


def test_provenance_of_a_missing_snapshot_is_null_not_an_exception(tmp_path):
    absent = arp_snapshot_provenance(tmp_path / "nope.txt")
    assert absent == {"path": str(tmp_path / "nope.txt"), "captured_at": None, "age_seconds": None, "stale": None}


def test_arp_snapshot_is_opt_in_and_absence_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOPOIESIS_ARP_SNAPSHOT_PATH", raising=False)
    assert load_arp_snapshot() == []
    assert load_arp_snapshot(tmp_path / "missing.txt") == []

    snapshot = tmp_path / "arp.txt"
    snapshot.write_text("192.168.1.23 dev eth2 lladdr d4:43:0e:1a:c5:88 REACHABLE\n")
    monkeypatch.setenv("AUTOPOIESIS_ARP_SNAPSHOT_PATH", str(snapshot))
    assert load_arp_snapshot() == [
        {"ip": "192.168.1.23", "mac": SQUATTER_MAC, "interface": "eth2"}
    ]


# ---------------------------------------------------------------------------
# unmanaged addresses -- the population the .23 incident came out of
# ---------------------------------------------------------------------------

def test_unmanaged_address_flags_in_scope_traffic_with_no_lease(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.50", "192.168.30.35"),
        ],
    )
    findings = detect_unmanaged_address(sweep_corpus(paths))

    assert [f["subject"] for f in findings] == ["192.168.1.50"]
    assert findings[0]["fault_class"] == "address_unmanaged"
    assert findings[0]["measured"]["role"] == "unmanaged_host"


def test_unmanaged_address_skips_leased_structural_and_out_of_scope_addresses(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.8", "192.168.1.255"),
            _flow("2026-06-16", "00:00:02", "10.9.9.9", "192.168.1.8"),
        ],
    )
    assert detect_unmanaged_address(sweep_corpus(paths)) == []


def test_gateway_address_is_reported_but_not_at_host_severity(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.1", "192.168.1.8"),
        ],
    )
    findings = detect_unmanaged_address(sweep_corpus(paths))
    assert [f["severity"] for f in findings] == ["low"]
    assert findings[0]["measured"]["role"] == "gateway"


# ---------------------------------------------------------------------------
# lease churn
# ---------------------------------------------------------------------------

def test_lease_churn_is_reported_per_segment_with_the_worst_hosts_attached(tmp_path):
    lines = [_ack("2026-06-16", f"00:00:{second:02d}", "192.168.1.9", "02:aa:bb:cc:dd:01") for second in range(0, 40, 10)]
    lines.append(_ack("2026-06-16", "00:00:00", "192.168.1.10", "02:aa:bb:cc:dd:02"))
    findings = detect_lease_churn(sweep_corpus(_corpus(tmp_path, lines)))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["subject"] == "192.168.1.0/24"
    assert finding["subject_kind"] == "segment"
    assert finding["measured"]["churning_hosts"] == 1
    assert finding["measured"]["leased_hosts"] == 2
    assert finding["measured"]["worst"][0]["ip"] == "192.168.1.9"


def test_lease_churn_ignores_renewals_that_respect_the_lease(tmp_path):
    lines = [
        _ack("2026-06-16", "00:00:00", "192.168.1.9", "02:aa:bb:cc:dd:01", lease=60),
        _ack("2026-06-16", "00:00:30", "192.168.1.9", "02:aa:bb:cc:dd:01", lease=60),
        _ack("2026-06-16", "00:01:00", "192.168.1.9", "02:aa:bb:cc:dd:01", lease=60),
        _ack("2026-06-16", "00:01:30", "192.168.1.9", "02:aa:bb:cc:dd:01", lease=60),
    ]
    assert detect_lease_churn(sweep_corpus(_corpus(tmp_path, lines))) == []


# ---------------------------------------------------------------------------
# remaining detectors
# ---------------------------------------------------------------------------

def test_host_multi_address_reports_one_row_per_mac(tmp_path):
    lines = [
        _ack("2026-06-16", "00:00:00", "192.168.1.8", "02:aa:bb:cc:dd:01"),
        _ack("2026-06-16", "00:00:01", "192.168.1.9", "02:aa:bb:cc:dd:01"),
        _ack("2026-06-16", "00:00:02", "192.168.1.10", "02:aa:bb:cc:dd:02"),
    ]
    findings = detect_host_multi_address(sweep_corpus(_corpus(tmp_path, lines)))
    assert [f["subject"] for f in findings] == ["02:aa:bb:cc:dd:01"]
    assert findings[0]["measured"]["addresses"] == ["192.168.1.8", "192.168.1.9"]


@pytest.mark.parametrize(
    "mac,expected",
    [
        ("02:aa:bb:cc:dd:01", True),
        ("6e:a2:a2:28:13:98", True),
        ("50:9a:4c:87:29:b3", False),
        ("d4:43:0e:1a:c5:88", False),
        ("zz:zz:zz:zz:zz:zz", False),
    ],
)
def test_locally_administered_bit_identifies_randomized_macs(mac, expected):
    assert _is_locally_administered(mac) is expected


def test_unmanaged_identity_is_a_segment_level_share_not_a_row_per_phone(tmp_path):
    lines = [
        _ack("2026-06-16", "00:00:00", "192.168.1.8", "02:aa:bb:cc:dd:01"),
        _ack("2026-06-16", "00:00:01", "192.168.1.9", "06:aa:bb:cc:dd:02"),
        _ack("2026-06-16", "00:00:02", "192.168.1.10", SERVER_MAC),
    ]
    findings = detect_unmanaged_identity(sweep_corpus(_corpus(tmp_path, lines)))
    assert len(findings) == 1
    assert findings[0]["measured"]["randomized_hosts"] == 2
    assert findings[0]["measured"]["leased_hosts"] == 3


def test_pool_pressure_only_fires_above_the_declared_ratio(tmp_path):
    lines = [
        _stats("2026-06-16", "00:00:00", "fortilink", 100, 95),
        _stats("2026-06-16", "00:00:01", "LACP", 100, 10),
    ]
    findings = detect_pool_pressure(sweep_corpus(_corpus(tmp_path, lines)))
    assert [f["subject"] for f in findings] == ["fortilink"]
    assert findings[0]["measured"]["utilisation"] == 0.95


def test_session_clash_counts_internal_sources_above_the_threshold(tmp_path):
    lines = [_clash("2026-06-16", "00:00:00", "192.168.1.16", "192.168.1.79") for _ in range(12)]
    findings = detect_session_clash(sweep_corpus(_corpus(tmp_path, lines)))
    assert {f["subject"] for f in findings} == {"192.168.1.16", "192.168.1.79"}
    assert all(f["measured"]["clash_events"] == 12 for f in findings)


def test_mgmt_bruteforce_collapses_a_rotating_campaign_into_one_finding(tmp_path):
    lines = [
        _admin_fail("2026-06-16", "00:00:00", f"93.152.221.{octet}")
        for octet in range(1, 9)
        for _ in range(5)
    ]
    findings = detect_mgmt_bruteforce(sweep_corpus(_corpus(tmp_path, lines)))

    assert len(findings) == 1
    assert findings[0]["subject"] == "management_plane"
    assert findings[0]["measured"]["failed_logins"] == 40
    assert findings[0]["measured"]["distinct_sources"] == 8
    assert findings[0]["measured"]["distinct_blocks"] == 1


# ---------------------------------------------------------------------------
# address space + whole report
# ---------------------------------------------------------------------------

def test_address_space_states_encode_how_each_address_is_known(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.8", SERVER_MAC),
            _ack("2026-06-16", "00:00:01", "192.168.1.23", SERVER_MAC),
            _flow("2026-06-16", "00:00:02", "192.168.1.50", "192.168.30.35"),
        ],
    )
    arp = [{"ip": "192.168.1.23", "mac": SQUATTER_MAC, "interface": "fortilink"}]
    segments = address_space(sweep_corpus(paths), arp)

    assert len(segments) == 1
    cells = {cell["ip"]: cell for cell in segments[0]["cells"]}
    assert cells["192.168.1.8"]["state"] == "leased"
    assert cells["192.168.1.23"]["state"] == "contested"
    assert cells["192.168.1.50"]["state"] == "unbound"
    assert cells["192.168.1.200"]["state"] == "silent"
    assert segments[0]["counts"]["contested"] == 1
    # 254 host addresses per /24 -- the blind region is drawn at true scale.
    assert len(segments[0]["cells"]) == 254


def test_report_is_deterministic_and_never_claims_to_be_a_live_reading(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.50", "192.168.30.35"),
            _stats("2026-06-16", "00:00:02", "fortilink", 100, 95),
        ],
    )
    pinned = {"available": False}
    first = build_environment_report(paths, flow_store=pinned, now=_now())
    second = build_environment_report(paths, flow_store=pinned, now=_now())

    assert first == second
    assert first["current_online_observation"] is False
    assert all(f["current_online_observation"] is False for f in first["findings"])
    assert first["totals"]["blind_classes"] >= 1
    assert first["sensors"]["l2_identity_records"] == 0


def test_report_severity_order_puts_critical_first(tmp_path):
    lines = [_admin_fail("2026-06-16", "00:00:00", "93.152.221.5") for _ in range(30)]
    lines.append(_ack("2026-06-16", "00:00:01", "192.168.1.8", "02:aa:bb:cc:dd:01"))
    report = build_environment_report(_corpus(tmp_path, lines))

    severities = [f["severity"] for f in report["findings"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert severities == sorted(severities, key=lambda level: order[level])


def test_every_finding_carries_its_limits_and_a_next_probe(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _flow("2026-06-16", "00:00:01", "192.168.1.50", "192.168.30.35"),
        ],
    )
    report = build_environment_report(
        paths,
        arp_snapshot_path=None,
    )
    assert report["findings"], "corpus should produce at least one finding"
    for finding in report["findings"]:
        assert finding["cannot_prove"], finding["finding_id"]
        assert finding["next_probe"], finding["finding_id"]
        assert 0.0 < finding["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# the console renders one language at a time
# ---------------------------------------------------------------------------

def test_every_playbook_step_has_a_chinese_rendering():
    missing = [
        what
        for spec in _PLAYBOOKS.values()
        for steps in (spec["verify"], spec["fix"])
        for _risk, what, _cmd in steps
        if what not in _PLAYBOOK_ZH
    ]
    assert missing == [], f"playbook steps without a zh rendering: {missing}"


def test_every_limit_a_detector_can_emit_has_a_chinese_rendering(tmp_path):
    corpus = _corpus(
        tmp_path,
        [
            _ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC),
            _ack("2026-06-16", "00:00:30", "192.168.1.23", "02:11:22:33:44:55"),
            _ack("2026-06-16", "00:00:40", "192.168.1.9", "06:aa:bb:cc:dd:02"),
            _ack("2026-06-16", "00:00:50", "192.168.1.10", "06:aa:bb:cc:dd:02"),
            _flow("2026-06-16", "00:00:01", "192.168.1.50", "192.168.30.35"),
            _stats("2026-06-16", "00:00:02", "fortilink", 100, 95),
        ]
        + [_ack("2026-06-16", f"00:01:{s:02d}", "192.168.1.9", "06:aa:bb:cc:dd:02") for s in range(0, 40, 10)]
        + [_clash("2026-06-16", "00:00:03", "192.168.1.16", "192.168.1.79") for _ in range(12)]
        + [_admin_fail("2026-06-16", "00:00:04", "93.152.221.5") for _ in range(30)],
    )
    report = build_environment_report(corpus, flow_store={"available": False}, now=_now())
    emitted = {limit for finding in report["findings"] for limit in finding["cannot_prove"]}
    assert emitted, "corpus should exercise several detectors"
    missing = sorted(limit for limit in emitted if limit not in _LIMIT_ZH)
    assert missing == [], f"limits without a zh rendering: {missing}"
    for finding in report["findings"]:
        assert len(finding["cannot_prove_zh"]) == len(finding["cannot_prove"])
        assert all(step.get("what_zh") for step in finding["playbook"]["verify"])


def test_every_verdict_carries_a_chinese_note(tmp_path):
    corpus = _corpus(tmp_path, [_ack("2026-06-16", "00:00:00", "192.168.1.23", SERVER_MAC)])
    ledger = _ledger(tmp_path, [("2026-08-06T09:50:00Z", SQUATTER_MAC)])
    report = build_environment_report(
        corpus, l2_ledger_path=ledger, flow_store={"available": False}, now=_now()
    )
    for finding in report["findings"]:
        assert finding["verification"]["note_zh"], finding["finding_id"]


def test_every_blind_class_remedy_has_a_chinese_rendering():
    missing = [
        spec["closes_with"]
        for spec in FAULT_CLASS_REQUIREMENTS.values()
        if spec["closes_with"] not in _CLOSES_WITH_ZH
    ]
    assert missing == [], f"remedies without a zh rendering: {missing}"
