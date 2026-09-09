"""Contract checks for local benchmark scenario records."""
from __future__ import annotations

import pytest

from core.evolve.replay_stream import _load_cases, case_fault_events

_cases, _gt = _load_cases()
if not _cases:
    pytest.skip(
        "real held-out dataset not present (local-only, gitignored)",
        allow_module_level=True,
    )


def test_every_local_replay_event_carries_scenario_and_entity_key():
    cases, _gt = _load_cases()
    for case in cases:
        events = case_fault_events(case)
        assert events
        for event in events:
            assert event["src_device_key"] == "DAHUA_FORTIGATE"
            assert event["fault_context"] == {
                "is_fault": True,
                "scenario": case.id,
                "confidence": 1.0,
            }
            assert event["event_ts"]
            assert event["type"]
            assert event["subtype"]
            assert event["replay"] is True
            assert event["source_kind"] == "simulated"
            assert event["case_id"] == case.id
