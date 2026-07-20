"""Falsifiable tests for the real-syslog RCA path.

These use a SYNTHETIC FortiOS-shaped stats blob (no company data) so they are
deterministic and committable. The real held-out eval runs against the local,
gitignored R230 dataset via `eval_real_heldout` and is reported separately.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domains.network_rca.adapters.real_syslog_adapter import RealSyslogAdapter
from domains.network_rca.eval import compare_baselines
from domains.network_rca.real_data_readiness import probe_r230_readiness
from domains.network_rca.schema import RCAGroundTruth, RCASeedCase


# Synthetic aggregates with both a dominant brute-force signal and a deny flood.
SYNTH_STATS = {
    "window_days": ["2026-01-01", "2026-01-02"],
    "admin_login_failed": 50000,
    "admin_login_failed_distinct_src": 120,
    "admin_login_failed_top_src": [["203.0.113.7", 1361], ["203.0.113.8", 1200]],
    "admin_login_disabled_lockouts": 9,
    "deny_count": 500000,
    "deny_top_dstports": [["137", 100000], ["5050", 40000]],
    "deny_top_src": [["192.168.16.10", 200000], ["192.168.1.50", 90000]],
    "accept_permit_count": 8000,
    "session_clash": 42,
    "fortigate_update_succeeded": 3,
}


def _stats_path(tmp_path: Path) -> Path:
    p = tmp_path / "real_window_stats.json"
    p.write_text(json.dumps(SYNTH_STATS), encoding="utf-8")
    return p


def _case(cid, terms, skills):
    return RCASeedCase(
        id=cid, title=cid, query=cid, query_terms=terms, assets=["fortigate"], relevant_skills=skills
    )


def _gt(cid, evidence, key):
    return RCAGroundTruth(
        case_id=cid, required_evidence=evidence, expected_root_cause_key=key, split="heldout", dataset_kind="real"
    )


def test_adapter_evidence_is_derived_from_real_aggregates(tmp_path):
    adapter = RealSyslogAdapter(SYNTH_STATS)
    auth = adapter.query("c", "admin_auth_failures")[0]
    assert auth["evidence_id"] == "ev-admin-auth-failures"
    assert auth["data"]["failed_login_count"] == 50000
    deny = adapter.query("c", "policy_deny_profile")[0]
    assert deny["data"]["deny_count"] == 500000
    assert deny["data"]["internal_src_ratio"] >= 0.5  # both top sources are internal
    assert adapter.query("c", "unknown_op") == []


def test_skill_control_matters_on_dominant_signal(tmp_path):
    # Two cases: brute-force (admin) and deny. Both stats carry a dominant
    # brute-force signal, so exposing ALL skills must misclassify the deny case.
    cases = [
        _case("bf", ["admin", "login", "failed", "lockout", "bruteforce"], ["check_admin_auth_failures", "check_admin_lockout"]),
        _case("deny", ["deny", "policy", "port", "traffic", "netbios"], ["check_policy_deny_profile", "check_traffic_baseline"]),
    ]
    gt = {
        "bf": _gt("bf", ["ev-admin-auth-failures", "ev-admin-lockout"], "admin_bruteforce_lockout"),
        "deny": _gt("deny", ["ev-policy-deny-profile", "ev-traffic-baseline"], "internal_policy_deny_expected"),
    }
    rows = {r.name: r for r in compare_baselines(cases, gt, data_source="real", real_stats_path=_stats_path(tmp_path))}
    # Skill controller present -> both cases correct.
    assert rows["autopoiesis_light_path"].root_cause_accuracy == 1.0
    # No skill control -> dominant brute-force evidence swamps the deny case.
    assert rows["full_tools"].root_cause_accuracy < 1.0


def test_benign_event_only_case_is_not_misclassified(tmp_path):
    # A pure event-log case must NOT be classified as a fault.
    from domains.network_rca.factory import build_network_rca_orchestrator

    case = _case("clash", ["event", "session", "clash"], ["check_event_log"])
    orch = build_network_rca_orchestrator(
        tmp_path / "ledger.jsonl", data_source="real", real_stats_path=_stats_path(tmp_path)
    )
    diagnosis, _ = orch.diagnose(case)
    assert diagnosis.root_cause_key == "benign_session_clash"


def test_readiness_blocked_without_manifest(tmp_path):
    report = probe_r230_readiness(manifest_path=tmp_path / "missing-manifest.json")
    assert report.blocked is True
    assert report.manifest_valid is False


def test_data_source_real_requires_stats_path(tmp_path):
    from domains.network_rca.factory import build_network_rca_orchestrator

    with pytest.raises(ValueError):
        build_network_rca_orchestrator(tmp_path / "l.jsonl", data_source="real", real_stats_path=None)
