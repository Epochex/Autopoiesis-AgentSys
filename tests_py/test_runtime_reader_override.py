"""Contract test for the ``runtime_dir`` override on ``load_runtime_snapshot``.

The benchmark scenario points the same live-situation reader at isolated replay
output. That is done by passing an explicit ``runtime_dir`` that overrides
``settings.stream_output_dir``. These tests build a
real-shaped alert + suggestion sink in a temp dir and assert the reader reads from
the OVERRIDE dir, and that a non-existent override degrades to "no live data" without
raising.
"""
from __future__ import annotations

import json
from pathlib import Path

from frontend.gateway.app.config import Settings
from frontend.gateway.app.runtime_reader import load_runtime_snapshot


def _write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def _seed_runtime(root: Path) -> None:
    """Write one real-shaped alert and one real-shaped suggestion into `root`."""
    _write_jsonl(
        root / "alerts" / "alerts-20260101-00.jsonl",
        {
            "alert_id": "alert-override-1",
            "alert_ts": "2026-01-01T00:00:05+00:00",
            "severity": "critical",
            "src_device_key": "DAHUA_FORTIGATE",
            "rule_id": "annotated_fault_v1",
            "dimensions": {"fault_scenario": "real_internal_deny_flood"},
        },
    )
    _write_jsonl(
        root / "aiops" / "suggestions-20260101-00.jsonl",
        {
            "suggestion_id": "sugg-override-1",
            "suggestion_ts": "2026-01-01T00:00:10+00:00",
            "suggestion_scope": "cluster",
            "alert_id": "alert-override-1",
            "rule_id": "annotated_fault_v1",
            "severity": "critical",
            "priority": "P1",
            "summary": "Denied internal flood cluster under review on DAHUA_FORTIGATE.",
            "context": {"service": "netbios-ns", "src_device_key": "DAHUA_FORTIGATE"},
            "confidence": 0.82,
            "confidence_label": "high",
        },
    )


def test_runtime_dir_override_reads_from_the_given_dir(tmp_path):
    override = tmp_path / "autopoiesis-stream-replay"
    _seed_runtime(override)

    # The configured stream directory points elsewhere; the override wins.
    settings = Settings.from_env()

    snapshot = load_runtime_snapshot(settings, "zh", runtime_dir=override)

    assert snapshot["ready"] is True
    assert snapshot["feed"], "feed must carry the seeded alert + suggestion"
    assert snapshot["suggestions"], "the seeded suggestion must surface"

    # Proof it read the OVERRIDE dir, not the default prod sink.
    sugg = snapshot["suggestions"][0]
    assert sugg["id"] == "sugg-override-1"
    assert sugg["scope"] == "cluster"
    assert sugg["deviceKey"] == "DAHUA_FORTIGATE"
    feed_ids = {item["id"] for item in snapshot["feed"]}
    assert "feed-alert-alert-override-1" in feed_ids
    assert "feed-suggestion-sugg-override-1" in feed_ids


def test_nonexistent_runtime_dir_degrades_without_raising(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert not missing.exists()

    snapshot = load_runtime_snapshot(Settings.from_env(), "zh", runtime_dir=missing)

    assert snapshot["ready"] is False
    assert snapshot["feed"] == []
    assert snapshot["suggestions"] == []


def test_recent_rotated_sink_files_remain_visible_to_case_sync(tmp_path):
    override = tmp_path / "autopoiesis-stream-replay"
    _seed_runtime(override)
    _write_jsonl(
        override / "alerts" / "alerts-20260101-01.jsonl",
        {
            "alert_id": "alert-override-2",
            "alert_ts": "2026-01-01T01:00:05+00:00",
            "severity": "warning",
            "src_device_key": "wan-source",
            "rule_id": "deny_burst_v1",
            "event_excerpt": {
                "srcip": "8.8.8.8", "dstip": "192.0.2.10", "service": "tcp/5555",
                "action": "deny", "subtype": "local", "policytype": "local-in-policy",
                "policyid": 0, "srcintf": "wan1", "srcintfrole": "wan",
            },
        },
    )
    _write_jsonl(
        override / "aiops" / "suggestions-20260101-01.jsonl",
        {
            "suggestion_id": "sugg-override-2",
            "suggestion_ts": "2026-01-01T01:00:10+00:00",
            "suggestion_scope": "cluster",
            "alert_id": "alert-override-2",
            "rule_id": "deny_burst_v1",
            "severity": "warning",
            "priority": "P2",
            "context": {"service": "tcp/5555", "src_device_key": "wan-source"},
        },
    )

    snapshot = load_runtime_snapshot(Settings.from_env(), "zh", runtime_dir=override)

    assert {item["id"] for item in snapshot["suggestions"]} == {
        "sugg-override-1", "sugg-override-2",
    }
    assert snapshot["defaultSuggestionId"] == "sugg-override-2"
