from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.evolve.memory_ops import is_stale, observe_fact, staleness
from core.memory.store import MemoryRecord, TieredMemoryStore


def _at(second: int) -> datetime:
    return datetime(2026, 8, 22, 12, 0, second, tzinfo=timezone.utc)


def test_repeated_same_value_updates_confirmation_without_records_or_ops():
    memory = TieredMemoryStore()
    recorder: list[dict] = []
    memory_id = observe_fact(
        memory,
        subject="eth2",
        relation="carrier_address",
        value="00:11:22:33:44:55",
        observed_at=_at(0),
        recorder=recorder,
    )
    assert [event["op"] for event in recorder] == ["ADD"]

    for observation in range(1, 101):
        assert observe_fact(
            memory,
            subject="eth2",
            relation="carrier_address",
            value="00:11:22:33:44:55",
            observed_at=_at(0) + timedelta(seconds=15 * observation),
            recorder=recorder,
        ) == memory_id

    assert len(memory.records()) == 1
    assert memory.get(memory_id).last_observed_at == _at(0) + timedelta(seconds=1500)
    assert [event["op"] for event in recorder] == ["ADD"]


def test_changed_value_revokes_old_interval_without_deleting_history():
    memory = TieredMemoryStore()
    recorder: list[dict] = []
    old_time = _at(0)
    changed_at = old_time + timedelta(minutes=5)
    old_id = observe_fact(
        memory,
        subject="eth2",
        relation="carrier_address",
        value="00:11:22:33:44:55",
        observed_at=old_time,
        recorder=recorder,
    )
    recorder.clear()

    new_id = observe_fact(
        memory,
        subject="eth2",
        relation="carrier_address",
        value="66:77:88:99:aa:bb",
        observed_at=changed_at,
        recorder=recorder,
    )

    assert len(memory.records()) == 2
    assert memory.get(old_id) is not None
    assert memory.get(old_id).valid_to == changed_at
    assert memory.get(new_id).valid_from == changed_at
    assert len(recorder) == 1
    assert recorder[0]["op"] == "REVOKE"
    assert recorder[0]["memory_id"] == old_id
    assert recorder[0]["target_id"] == new_id

    before = memory.retrieve(
        ["carrier_address"], ["eth2"], limit_per_tier=5, as_of=old_time
    )["semantic"]
    now = memory.retrieve(
        ["carrier_address"], ["eth2"], limit_per_tier=5, as_of=changed_at
    )["semantic"]
    assert [record.memory_id for record in before] == [old_id]
    assert [record.memory_id for record in now] == [new_id]


def test_freshness_is_monotonic_bounded_and_ttl_boundary_is_inclusive():
    confirmed_at = _at(0)
    record = MemoryRecord(
        memory_id="fact-test",
        tier="semantic",
        text="eth2 carrier up",
        last_observed_at=confirmed_at,
        valid_from=confirmed_at,
    )

    # The horizon is named here rather than inherited from the tier. It used to
    # be one global 60s constant applied to everything, which was right for an
    # environment fact re-read every 15s and catastrophic for a learned method —
    # see _STALE_AFTER_SEC. The shape of the curve is what this test pins.
    scores = [
        staleness(record, now=confirmed_at + timedelta(seconds=seconds), horizon_sec=60.0)
        for seconds in (0, 15, 30, 60, 600)
    ]
    assert scores == [0.0, 0.25, 0.5, 1.0, 1.0]
    assert scores == sorted(scores)
    assert not is_stale(record, now=confirmed_at + timedelta(seconds=59), ttl_sec=60)
    assert is_stale(record, now=confirmed_at + timedelta(seconds=60), ttl_sec=60)

    memory = TieredMemoryStore()
    record.tags = ["carrier"]
    memory.add(record)
    recalled = memory.retrieve(
        ["carrier"], [], as_of=confirmed_at + timedelta(seconds=600)
    )["semantic"]
    assert recalled == [record]


def test_revoked_or_unconfirmed_records_are_stale_but_remain_addressable():
    now = _at(0)
    revoked = MemoryRecord(
        memory_id="revoked",
        tier="semantic",
        text="old value",
        last_observed_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(minutes=1),
        valid_to=now,
    )
    unconfirmed = MemoryRecord(memory_id="unknown", tier="semantic", text="unknown")
    memory = TieredMemoryStore()
    memory.seed([revoked, unconfirmed])

    assert staleness(revoked, now=now) == 1.0
    assert is_stale(revoked, now=now, ttl_sec=60)
    assert staleness(unconfirmed, now=now) == 1.0
    assert is_stale(unconfirmed, now=now, ttl_sec=60)
    assert memory.get("revoked") is revoked
    assert memory.get("unknown") is unconfirmed


def test_negative_ttl_is_rejected():
    record = MemoryRecord(memory_id="fact-test", tier="semantic", text="fact")
    with pytest.raises(ValueError, match="ttl_sec"):
        is_stale(record, now=_at(0), ttl_sec=-1)


# ── the horizon was one global number, and it disabled the feature ───────────

def test_a_method_learned_an_hour_ago_is_not_fully_stale():
    """60s applied to every tier meant any learned method was dead on arrival.

    Procedural memories carry the timestamp of the incident that taught them, so
    a single global horizon sized for a 15-second environment poll drove their
    routing weight to zero within a minute — the probe prior could never fire on
    anything the system had actually learned.
    """
    from datetime import datetime, timedelta, timezone

    from core.memory.store import MemoryRecord
    from core.evolve.memory_ops import staleness

    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    learned = now - timedelta(hours=1)
    proc = MemoryRecord(memory_id="p", tier="procedural", text="probe carrier first",
                        last_observed_at=learned)
    env = MemoryRecord(memory_id="a", tier="asset_profile", text="eth2 carries the address",
                       last_observed_at=learned)

    assert staleness(proc, now=now) < 0.01, "a method does not rot in an hour"
    assert staleness(env, now=now) == 1.0, "an env fact re-read every 15s does"


def test_the_horizon_can_be_named_by_the_caller():
    from datetime import datetime, timedelta, timezone

    from core.memory.store import MemoryRecord
    from core.evolve.memory_ops import staleness

    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    rec = MemoryRecord(memory_id="p", tier="procedural", text="x",
                       last_observed_at=now - timedelta(seconds=50))
    assert staleness(rec, now=now, horizon_sec=100.0) == 0.5
