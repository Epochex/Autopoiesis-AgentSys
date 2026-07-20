"""Falsifiable tests for the fault-injection + real-detection demo harness.

These verify that the injectors are (1) deterministic, (2) emit syslog that the
PRODUCTION FortiOS parser can consume, (3) produce stats DERIVED from the emitted
lines, and (4) drive a genuine end-to-end detection of the injected fault through
the existing RCA pipeline — with the brute-force and port-probe cases explicitly
required, plus a negative control proving the verdict tracks the injected data.
"""
from __future__ import annotations

import pytest

from core.eval.demo_detection import run_detection
from domains.network_rca.adapters.fortios_syslog import parse_fortios_kv_line
from domains.network_rca.fault_injection import (
    DVR_PORTS,
    INJECTORS,
    aggregate_window_stats,
    inject_admin_bruteforce_lockout,
    inject_all,
    inject_clean_baseline,
    inject_device_port_probe,
    parse_line,
)


# ── 1. determinism ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(INJECTORS))
def test_injectors_are_deterministic(name):
    factory = INJECTORS[name]
    a = factory()
    b = factory()
    assert a.syslog_lines == b.syslog_lines
    assert a.window_stats() == b.window_stats()
    assert a.expected_root_cause_key == b.expected_root_cause_key


def test_distinct_incident_types_are_distinct():
    incidents = inject_all()
    assert len({i.incident_type for i in incidents}) == len(incidents)
    # different incidents produce different syslog corpora
    assert incidents[0].syslog_lines != incidents[1].syslog_lines


# ── 2. the emitted syslog is real-shaped and PRODUCTION-parseable ───────────────
@pytest.mark.parametrize("name", sorted(INJECTORS))
def test_lines_parse_with_production_parser(name):
    incident = INJECTORS[name]()
    # A sample of every injector's lines must be consumable by the production
    # shlex-based FortiOS parser (domains/network_rca/adapters/fortios_syslog.py),
    # not just the fast regex aggregator — proving they are genuinely real-shaped.
    for line in incident.syslog_lines[:60]:
        event = parse_fortios_kv_line(line)  # must not raise
        assert event.fields.get("devid") == "FG100ETK20014183"
        assert event.fields.get("logid")  # every line carries a FortiGate logid
        # the two parsers agree on the load-bearing structured fields
        regex_fields = parse_line(line)
        for key in ("logid", "action", "srcip", "dstport", "logdesc"):
            assert regex_fields.get(key) == event.fields.get(key)


def test_port_probe_lines_target_dvr_ports():
    incident = inject_device_port_probe()
    probe_lines = [ln for ln in incident.syslog_lines if 'action="deny"' in ln and 'Dahua SDK' in ln]
    assert probe_lines
    for line in probe_lines:
        assert parse_line(line)["dstport"] in DVR_PORTS


# ── 3. stats are DERIVED from the emitted lines (not hand-written) ──────────────
def test_aggregate_is_derived_from_lines():
    incident = inject_admin_bruteforce_lockout()
    stats = incident.window_stats()
    # the failed-login count equals the number of "Admin login failed" lines actually emitted
    emitted_fails = sum(1 for ln in incident.syslog_lines if 'logdesc="Admin login failed"' in ln)
    assert stats["admin_login_failed"] == emitted_fails > 0

    # mutate the evidence: append one more attacker line -> the derived stat MUST move
    extra = incident.syslog_lines + [incident.syslog_lines[0]]
    assert aggregate_window_stats(extra)["admin_login_failed"] == emitted_fails + 1


def test_aggregate_matches_repo_classifier():
    """Our aggregator's category counts match the repo's authoritative classifier."""
    corpus = pytest.importorskip("core.eval.fortigate_corpus_retrieval")
    incident = inject_device_port_probe()
    stats = incident.window_stats()
    rows = [corpus.parse_syslog_line(ln) for ln in incident.syslog_lines]
    cats = [corpus.classify_category(r) for r in rows]
    assert stats["device_service_port_deny"] == cats.count("device_port_probe")
    assert stats["deny_count"] == cats.count("policy_deny") + cats.count("device_port_probe")


def test_injected_stats_clear_reasoner_thresholds():
    from domains.network_rca.fault_injection import inject_internal_policy_deny_flood

    bf = inject_admin_bruteforce_lockout().window_stats()
    assert bf["admin_login_failed"] >= 1000 and bf["admin_login_disabled_lockouts"] >= 1

    flood = inject_internal_policy_deny_flood().window_stats()
    assert flood["deny_count"] >= 10000
    internal = [ip for ip, _ in flood["deny_top_src"] if str(ip).startswith("192.168.")]
    assert len(internal) / max(1, len(flood["deny_top_src"])) >= 0.5

    probe = inject_device_port_probe().window_stats()
    assert probe["device_service_port_deny"] > 0


# ── 4. REAL end-to-end detection through the existing pipeline ──────────────────
def test_admin_bruteforce_detects_end_to_end():
    result = run_detection(inject_admin_bruteforce_lockout())
    assert result.detected_key == "admin_bruteforce_lockout"
    assert result.matched and result.verifier_passed
    assert result.probes >= 1
    assert "check_admin_auth_failures" in result.exposed_skills


def test_device_port_probe_detects_end_to_end():
    result = run_detection(inject_device_port_probe())
    assert result.detected_key == "device_service_port_probe_contained"
    assert result.matched and result.verifier_passed
    assert "check_device_port_probe" in result.exposed_skills


@pytest.mark.parametrize("name", sorted(INJECTORS))
def test_every_incident_type_detects_end_to_end(name):
    result = run_detection(INJECTORS[name]())
    assert result.matched, f"{name}: expected {result.expected_key}, got {result.detected_key}"
    assert result.verifier_passed


def test_negative_control_clean_window_localizes_no_fault():
    """The fault query on a fault-free window must NOT localize the fault.

    This is the anti-puppet-show check: detection tracks the injected DATA, so the
    identical operator query yields 'unknown' when the fault signal is absent.
    """
    clean_stats = aggregate_window_stats(inject_clean_baseline())
    for factory in (inject_admin_bruteforce_lockout, inject_device_port_probe):
        incident = factory()
        result = run_detection(incident, stats_override=clean_stats)
        assert result.detected_key != incident.expected_root_cause_key
        assert result.detected_key == "unknown"
