"""The two reads that used to parse the whole store per request.

On 10 Sep the trace ledger was 952 MB and the session directory 1.5 GB; the
memory receipt replayed the first and parsed the newest thousand of the second
on every 5-second poll, which took the gateway from 260 MB to 1.7 GB and
stalled it. These tests pin the incremental behaviour that replaced that.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

from core.trace.events import TraceEvent
from core.trace.ledger import JSONLTraceLedger
from frontend.gateway.app import main as gateway
from frontend.gateway.app import investigation_session_store as store_module
from frontend.gateway.app.investigation_session_store import InvestigationSessionStore


def _event(kind: str, memory_id: str | None = None) -> TraceEvent:
    payload = {"memory_id": memory_id} if memory_id else {}
    return TraceEvent(run_id="r", case_id="c", kind=kind, payload=payload)


def _reset_ledger_state():
    gateway._TRACE_INFLUENCE_STATE.update(path=None, inode=None, offset=0, events=[])


def test_ledger_tail_returns_only_influence_kinds_and_reads_incrementally(tmp_path, monkeypatch):
    _reset_ledger_state()
    ledger = JSONLTraceLedger(tmp_path / "trace.jsonl", durable=False)
    ledger.append(_event("working_set_updated"))
    ledger.append(_event("memory_attributed", "mem-1"))
    ledger.append(_event("hypothesis_proposed"))
    monkeypatch.setattr(gateway, "_evolving_service", None)
    monkeypatch.setattr(gateway, "settings", dataclasses.replace(gateway.settings, trace_ledger_path=ledger.path))

    first = gateway._memory_trace_events()
    assert [e.kind for e in first] == ["memory_attributed"]
    offset_after_first = gateway._TRACE_INFLUENCE_STATE["offset"]
    assert offset_after_first == ledger.path.stat().st_size

    # Nothing new: no bytes are read and the same list comes back.
    assert [e.kind for e in gateway._memory_trace_events()] == ["memory_attributed"]

    # Appends are picked up from the recorded offset only.
    ledger.append(_event("working_set_updated"))
    ledger.append(_event("memory_shortcut", "mem-2"))
    later = gateway._memory_trace_events()
    assert [e.payload["memory_id"] for e in later] == ["mem-1", "mem-2"]
    assert gateway._TRACE_INFLUENCE_STATE["offset"] == ledger.path.stat().st_size


def test_ledger_tail_ignores_a_partial_trailing_line_and_restarts_on_rotation(tmp_path, monkeypatch):
    _reset_ledger_state()
    path = tmp_path / "trace.jsonl"
    monkeypatch.setattr(gateway, "_evolving_service", None)
    monkeypatch.setattr(gateway, "settings", dataclasses.replace(gateway.settings, trace_ledger_path=path))
    full = _event("memory_resolved", "mem-1").model_dump_json() + "\n"
    partial = _event("memory_resolved", "mem-2").model_dump_json()[:40]  # writer mid-line
    path.write_text(full + partial, encoding="utf-8")
    assert [e.payload["memory_id"] for e in gateway._memory_trace_events()] == ["mem-1"]
    assert gateway._TRACE_INFLUENCE_STATE["offset"] == len(full.encode("utf-8"))

    # The writer finishes the line: only the completed tail is parsed.
    path.write_text(full + _event("memory_resolved", "mem-2").model_dump_json() + "\n", encoding="utf-8")
    assert [e.payload["memory_id"] for e in gateway._memory_trace_events()] == ["mem-1", "mem-2"]

    # Rotation (a shorter file) starts the read over instead of seeking past its end.
    path.write_text(_event("memory_attributed", "mem-9").model_dump_json() + "\n", encoding="utf-8")
    assert [e.payload["memory_id"] for e in gateway._memory_trace_events()] == ["mem-9"]


def test_ledger_object_without_a_file_is_replayed_once_and_filtered(monkeypatch):
    _reset_ledger_state()
    events = [_event("working_set_updated"), _event("memory_attributed", "mem-1")]
    ledger = SimpleNamespace(replay=lambda: list(events))
    monkeypatch.setattr(gateway, "_evolving_service", SimpleNamespace(orchestrator=SimpleNamespace(ledger=ledger)))
    assert [e.kind for e in gateway._memory_trace_events()] == ["memory_attributed"]


def _snapshot(session_id: str, **fields) -> dict:
    body = {"session_id": session_id, "subject": "unit.service", "case_id": "case-1",
            "evidence": "x" * 2000, "source_refs": []}
    body.update(fields)
    return body


def test_session_index_is_shared_across_store_instances_and_loads_only_the_match(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_SUMMARY_INDEXES", {})
    store = InvestigationSessionStore(tmp_path)
    store.save(_snapshot("old", case_id="case-0"))
    store.save(_snapshot("eval", evaluation_source_case_id="case-1"))
    store.save(_snapshot("live"))
    parsed: list[Path] = []
    original = InvestigationSessionStore._summary
    monkeypatch.setattr(InvestigationSessionStore, "_summary", lambda self, path: parsed.append(path) or original(self, path))

    chosen = InvestigationSessionStore(tmp_path).find_latest(subject="unit.service", incident_ref="case-1")
    assert chosen["session_id"] == "live"          # evaluation snapshot skipped
    assert len(parsed) == 3                         # first pass indexes every file once

    # A fresh store instance (one per request in the gateway) reuses the index.
    again = InvestigationSessionStore(tmp_path).find_latest(subject="unit.service", incident_ref="case-1")
    assert again["session_id"] == "live" and len(parsed) == 3

    # Only a changed file is parsed again, and the index follows the change.
    store.save(_snapshot("live", case_id="case-2"))
    assert InvestigationSessionStore(tmp_path).find_latest(subject="unit.service", incident_ref="case-1") is None
    assert len(parsed) == 4
    assert InvestigationSessionStore(tmp_path).find_latest(subject="unit.service")["session_id"] == "live"


def test_session_index_sidecar_survives_a_new_process(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_SUMMARY_INDEXES", {})
    store = InvestigationSessionStore(tmp_path)
    store.save(_snapshot("a", source_refs=[{"source_id": "evt-7"}]))
    store.summaries(10)
    sidecar = json.loads((tmp_path / ".summary-index.json").read_text(encoding="utf-8"))
    assert sidecar["files"]["a.json"]["summary"]["source_ids"] == ["evt-7"]

    # A new process (empty module cache) seeds from the sidecar: no file is parsed.
    monkeypatch.setattr(store_module, "_SUMMARY_INDEXES", {})
    monkeypatch.setattr(InvestigationSessionStore, "_summary", lambda self, path: (_ for _ in ()).throw(AssertionError("parsed")))
    found = InvestigationSessionStore(tmp_path).find_latest(subject="unit.service", incident_ref="evt-7")
    assert found["session_id"] == "a"


def test_display_payload_renders_without_a_model(monkeypatch):
    from frontend.gateway.app import display_translation as translation
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("no model")
    monkeypatch.setattr(translation, "translate_texts", unavailable)
    payload = {"cases": [{"title": "受管服务进入失败状态", "raw": {"title": "留"}}]}
    assert translation.translate_display_payload(payload) == payload
