"""The recurrence endpoint exposes the evidence behind the stop decision."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from core.remediate.sentinel import COOLDOWN_SEC
from frontend.gateway.app import main

NOW = time.time()


def _at(offset: int) -> str:
    return (datetime.fromtimestamp(NOW, tz=timezone.utc) + timedelta(seconds=offset)).isoformat()


def _detected(offset: int, subject: str) -> dict:
    return {
        "kind": "detected",
        "at": _at(offset),
        "detector": "failed_units",
        "subject": subject,
        "action": "restart_unit",
    }


def _resolved(offset: int, subject: str) -> dict:
    return {
        "kind": "resolved",
        "at": _at(offset),
        "detector": "failed_units",
        "subject": subject,
        "action": "restart_unit",
        "outcome": "passed",
        "samples": 12,
    }


def _chain(subject: str, recurrences: int, *, start: int = -1000) -> list[dict]:
    events = [_detected(start, subject)]
    for index in range(recurrences):
        resolved_at = start + index * 100 + 20
        events.extend([_resolved(resolved_at, subject), _detected(resolved_at + 20, subject)])
    return events


def _write(tmp_path, events: list[dict], monkeypatch) -> None:
    path = tmp_path / "sentinel-timeline.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(path))


def _get(monkeypatch) -> dict:
    async def run_inline(function, *args):
        return function(*args)

    # The endpoint still goes through `to_thread`; running that call inline here
    # keeps pytest's own event-loop plugin from competing over executor cleanup.
    monkeypatch.setattr(main.asyncio, "to_thread", run_inline)
    return asyncio.run(main.sentinel_recurrence())


def test_an_empty_timeline_has_no_recurrence_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_SENTINEL_TIMELINE", str(tmp_path / "absent.jsonl"))

    response = _get(monkeypatch)

    assert response["ok"] is True
    assert response["keys"] == []


def test_three_recurrences_expose_the_chain_and_escalate(tmp_path, monkeypatch):
    _write(tmp_path, _chain("demo-collector.service", 3), monkeypatch)

    response = _get(monkeypatch)

    history = response["keys"][0]
    assert history["key"] == "failed_units:demo-collector.service:restart_unit"
    assert history["recurrences"] == 3
    assert history["escalated"] is True
    assert len(history["cycles"]) == 3


def test_two_recurrences_wait_longer_without_escalating(tmp_path, monkeypatch):
    _write(tmp_path, _chain("demo-collector.service", 2), monkeypatch)

    history = _get(monkeypatch)["keys"][0]

    assert history["escalated"] is False
    assert history["next_cooldown_sec"] > COOLDOWN_SEC


def test_keys_are_sorted_by_recurrence_count(tmp_path, monkeypatch):
    events = (
        _chain("one.service", 1, start=-3000)
        + _chain("three.service", 3, start=-2000)
        + _chain("two.service", 2, start=-1000)
    )
    _write(tmp_path, events, monkeypatch)

    response = _get(monkeypatch)

    assert [item["recurrences"] for item in response["keys"]] == [3, 2, 1]
