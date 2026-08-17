"""Falsifiable tests for the injected-event schema + per-event event_id stamping.

These guard two recent additions to ``core.evolve.replay_stream``:

* Every event built by ``case_fault_events`` now carries the stable entity key
  ``src_device_key == "DAHUA_FORTIGATE"`` and a ``fault_context`` annotation
  (``is_fault``/``scenario``/``confidence``) so the REAL correlator's
  annotated-fault rule fires exactly once per scenario.
* ``produce_replay`` stamps a per-batch-unique ``event_id`` on every event so the
  correlator's QualityGate (which requires ``event_id, event_ts, type, subtype``)
  never drops a replayed fault as a dedup collision.

The events also have to satisfy the QualityGate's non-empty ``event_ts / type /
subtype`` requirement, so that is asserted here too.
"""
from __future__ import annotations

import uuid

import pytest

from core.evolve.replay_stream import (
    REPLAY_TOPIC,
    _load_cases,
    case_fault_events,
    produce_replay,
)

# Local-only held-out cases (gitignored); skip the module when absent (CI).
_cases, _gt = _load_cases()
if not _cases:
    pytest.skip(
        "real held-out dataset not present (local-only, gitignored)",
        allow_module_level=True,
    )


def test_every_injected_event_carries_fault_context_and_entity_key():
    cases, _gt = _load_cases()
    assert cases, "real held-out cases must load"
    for case in cases:
        events = case_fault_events(case)
        assert events, f"{case.id} yielded no events"
        for ev in events:
            # stable entity key -> one annotated_fault alert per scenario
            assert ev["src_device_key"] == "DAHUA_FORTIGATE"
            fc = ev["fault_context"]
            assert fc["is_fault"] is True
            assert fc["scenario"] == case.id
            assert fc["confidence"] == 1.0
            # QualityGate hard requirements (besides event_id, stamped at produce time)
            assert ev["event_ts"], "event_ts must be non-empty"
            assert ev["type"], "type must be non-empty"
            assert ev["subtype"], "subtype must be non-empty"
            # honesty markers preserved
            assert ev["replay"] is True
            assert ev["case_id"] == case.id


def _stamp_event_ids(events: list[dict]) -> str:
    """Replicate produce_replay's per-batch event_id stamping (kubectl-free)."""
    batch = uuid.uuid4().hex[:8]
    for i, ev in enumerate(events):
        ev["event_id"] = f"{ev.get('case_id', 'replay')}-{i:03d}-{batch}"
    return batch


def test_stamped_event_ids_are_unique_nonempty_and_case_prefixed():
    cases, _gt = _load_cases()
    assert cases, "real held-out cases must load"

    all_events: list[dict] = []
    for case in cases:
        all_events.extend(case_fault_events(case))

    _stamp_event_ids(all_events)

    ids = [ev["event_id"] for ev in all_events]
    assert all(ids), "no event_id may be empty"
    assert len(ids) == len(set(ids)), "every stamped event_id must be unique"
    for ev in all_events:
        assert ev["event_id"].startswith(ev["case_id"]), ev["event_id"]


def test_produce_replay_degrades_cleanly_without_kubectl(monkeypatch):
    # kubectl/rpk is unavailable in CI/sandbox: produce must degrade, never raise.
    monkeypatch.setattr("core.evolve.replay_stream._KUBECTL", "kubectl-does-not-exist-xyz")
    result = produce_replay()
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["produced"] == 0
    assert result["topic"] == REPLAY_TOPIC
