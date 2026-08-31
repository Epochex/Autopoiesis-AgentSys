"""Live-situation reader contract for Autopoiesis-owned stream output."""
from __future__ import annotations

import pytest

# runtime_reader depends only on config (plain stdlib) + landed Autopoiesis files, so it
# needs no fastapi — unlike the main-app gateway tests. Import directly.
from frontend.gateway.app.config import Settings
from frontend.gateway.app.runtime_reader import (
    build_runtime_stream_delta,
    load_runtime_snapshot,
)


def _has_suggestions() -> bool:
    settings = Settings.from_env()
    aiops = settings.stream_output_dir / "aiops"
    return aiops.is_dir() and any(aiops.glob("suggestions-*.jsonl"))


@pytest.mark.skipif(
    not _has_suggestions(),
    reason="no Autopoiesis suggestion fixture present",
)
def test_load_runtime_snapshot_exposes_detection_facts_without_draft_reasoning():
    settings = Settings.from_env()

    snapshot = load_runtime_snapshot(settings)

    suggestions = snapshot.get("suggestions", [])
    assert suggestions

    first = suggestions[0]
    assert first.get("sourceAlertIds")
    assert first.get("dataClassification") in {"observed", "controlled_test"}
    assert first["incidentFacts"]["sourceIp"]
    assert first["incidentFacts"]["destinationIp"]
    assert first["incidentFacts"]["service"]
    assert "stageTelemetry" not in first
    assert "hypothesisSet" not in first
    assert "runbookDraft" not in first
    assert "reviewVerdict" not in first


@pytest.mark.skipif(
    not _has_suggestions(),
    reason="no Autopoiesis suggestion fixture present",
)
def test_english_runtime_snapshot_localizes_all_visible_copy():
    snapshot = load_runtime_snapshot(Settings.from_env(), "en")

    visible: list[str] = []
    for item in snapshot["feed"]:
        visible.extend([item.get("device", ""), item.get("summary", "")])
    for item in snapshot["clusterWatch"]:
        visible.append(item.get("key", ""))
    for suggestion in snapshot["suggestions"]:
        visible.extend([
            suggestion["summary"],
            suggestion["device"],
        ])

    assert visible
    assert all(not any("\u3400" <= char <= "\u9fff" for char in text) for text in visible)


def test_build_runtime_stream_delta_from_new_alert_feed():
    previous = {
        "feed": [{"id": "feed-raw-1", "kind": "raw"}],
        "clusterWatch": [],
        "runtime": {"latestAlertTs": "n/a", "latestSuggestionTs": "n/a"},
        "defaultSuggestionId": "",
    }
    current = {
        "feed": [
            {"id": "feed-alert-1", "kind": "alert"},
            {"id": "feed-raw-1", "kind": "raw"},
        ],
        "clusterWatch": [],
        "runtime": {"latestAlertTs": "2026-03-25T12:00:00+00:00", "latestSuggestionTs": "n/a"},
        "defaultSuggestionId": "",
    }

    delta = build_runtime_stream_delta(previous, current)

    assert delta is not None
    assert delta["kind"] == "alert"
    assert delta["reason"] == "feed"
    assert delta["feedIds"] == ["feed-alert-1"]
    assert delta["stageIds"] == ["correlator", "alerts-topic", "cluster-window"]


def test_build_runtime_stream_delta_marks_cluster_suggestion_path():
    previous = {
        "feed": [],
        "clusterWatch": [],
        "runtime": {"latestAlertTs": "n/a", "latestSuggestionTs": "n/a"},
        "defaultSuggestionId": "",
    }
    current = {
        "feed": [{"id": "feed-suggestion-1", "kind": "suggestion", "scope": "cluster"}],
        "clusterWatch": [],
        "runtime": {
            "latestAlertTs": "2026-03-25T12:00:00+00:00",
            "latestSuggestionTs": "2026-03-25T12:00:01+00:00",
        },
        "defaultSuggestionId": "suggestion-1",
    }

    delta = build_runtime_stream_delta(previous, current)

    assert delta is not None
    assert delta["kind"] == "cluster"
    assert delta["stageIds"] == [
        "cluster-window",
        "case-investigation",
    ]


def test_build_runtime_stream_delta_returns_none_without_changes():
    snapshot = {
        "feed": [{"id": "feed-raw-1", "kind": "raw"}],
        "clusterWatch": [{"key": "a", "progress": 1, "target": 3}],
        "runtime": {
            "latestAlertTs": "2026-03-25T12:00:00+00:00",
            "latestSuggestionTs": "2026-03-25T12:00:01+00:00",
        },
        "defaultSuggestionId": "suggestion-1",
    }

    assert build_runtime_stream_delta(snapshot, snapshot) is None
