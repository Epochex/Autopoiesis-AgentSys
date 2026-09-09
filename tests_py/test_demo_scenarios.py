from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frontend.gateway.app import investigate, main
from frontend.gateway.app.demo_scenarios import append_run, recent_runs, scenario_snapshot


def _run(run_id: str, scenario: str, completed_at: str) -> dict:
    return {
        "runId": run_id,
        "scenario": scenario,
        "status": "passed",
        "startedAt": "2026-09-07T10:00:00+00:00",
        "completedAt": completed_at,
        "subject": "autopoiesis-acceptance-demo.service",
        "headline": "controlled acceptance passed",
        "metrics": {"savedProbes": 3},
    }


def test_demo_ledger_is_controlled_append_only_and_newest_first(monkeypatch, tmp_path):
    path = tmp_path / "demo-runs.jsonl"
    monkeypatch.setenv("AUTOPOIESIS_DEMO_RUNS_PATH", str(path))

    first = append_run(_run("one", "self_healing", "2026-09-07T10:00:01+00:00"))
    second = append_run(_run("two", "memory_trajectory", "2026-09-07T10:00:02+00:00"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("partial invalid line")

    rows = recent_runs(10)
    snapshot = scenario_snapshot()

    assert first["dataClassification"] == "controlled_test"
    assert second["dataClassification"] == "controlled_test"
    assert [item["runId"] for item in rows] == ["two", "one"]
    assert snapshot["latest"]["self_healing"]["runId"] == "one"
    assert snapshot["latest"]["memory_trajectory"]["runId"] == "two"
    encoded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[:2]]
    assert all(item["dataClassification"] == "controlled_test" for item in encoded)


def test_demo_api_returns_receipts_and_start_forwards_memory_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPOIESIS_DEMO_RUNS_PATH", str(tmp_path / "runs.jsonl"))
    append_run(_run("memory", "memory_trajectory", "2026-09-07T10:00:02+00:00"))
    captured: dict = {}

    def fake_start(*args, **kwargs):
        captured.update({"args": args, "kwargs": kwargs})
        return {"session_id": "session-demo"}

    monkeypatch.setattr(investigate, "start", fake_start)
    client = TestClient(main.app)

    listed = client.get("/api/rca/demo/runs")
    opened = client.post("/api/rca/investigate/start", json={
        "question": "controlled memory pass",
        "memory_enabled": False,
    })

    assert listed.status_code == 200
    assert listed.json()["latest"]["memory_trajectory"]["runId"] == "memory"
    assert opened.status_code == 200
    assert captured["kwargs"]["memory_enabled"] is False
