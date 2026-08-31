from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from domains.network_rca.risk_pattern import (
    RiskEvent,
    RiskPatternStore,
    risk_event_from_clickhouse_row,
)


UTC = timezone.utc


def _event(
    event_id: str,
    day: int = 1,
    *,
    source_ip: str = "45.74.28.226",
    provenance: str = "real",
    account: str = "mike",
    asset: str = "192.168.1.1",
    scope: str = "edge-fw-1",
    service: str = "HTTPS",
    risk_type: str = "credential_attack",
) -> RiskEvent:
    return RiskEvent(
        event_id=event_id,
        observed_at=datetime(2026, 8, day, 10, tzinfo=UTC),
        risk_type=risk_type,
        scope_key=scope,
        target_asset=asset,
        provenance=provenance,  # type: ignore[arg-type]
        source_ip=source_ip,
        target_account=account,
        target_service=service,
        evidence_ref=f"autopoiesis.facts:{event_id}",
    )


def test_aggregates_campaign_dimensions_without_creating_log_memories():
    store = RiskPatternStore()
    events = [
        _event("e1", 1, source_ip="45.74.28.226", account="mike"),
        _event("e2", 2, source_ip="45.74.28.227", account="admin"),
        _event(
            "e3",
            3,
            source_ip="77.91.118.34",
            account="mike",
            asset="192.168.1.2",
        ),
    ]

    affected = store.ingest_many(events)

    assert len(affected) == 1
    pattern = affected[0]
    assert len(store.list_patterns()) == 1
    assert pattern.event_count == 3
    assert pattern.active_days == 3
    assert pattern.first_seen == events[0].observed_at
    assert pattern.last_seen == events[-1].observed_at
    assert pattern.source_ips == ("45.74.28.226", "45.74.28.227", "77.91.118.34")
    assert pattern.source_networks == ("45.74.28.0/24", "77.91.118.0/24")
    assert pattern.target_accounts == ("admin", "mike")
    assert pattern.target_assets == ("192.168.1.1", "192.168.1.2")
    assert pattern.trend == "stable"


def test_update_is_idempotent_and_does_not_change_snapshot():
    store = RiskPatternStore()
    event = _event("same")
    store.ingest(event)
    before = store.snapshot_json()

    store.ingest(event)

    assert store.snapshot_json() == before
    assert store.list_patterns()[0].event_count == 1


def test_duplicate_batch_reports_no_affected_patterns():
    store = RiskPatternStore()
    event = _event("same-batch")
    assert len(store.ingest_many([event])) == 1

    assert store.ingest_many([event]) == []


def test_real_replay_and_drill_are_isolated_patterns():
    store = RiskPatternStore()

    store.ingest_many(
        [
            _event("real", provenance="real"),
            _event("replay", provenance="replay"),
            _event("drill", provenance="drill"),
        ]
    )

    rows = store.list_patterns()
    assert len(rows) == 3
    assert {item.provenance for item in rows} == {"real", "replay", "drill"}
    assert len({item.pattern_id for item in rows}) == 3
    assert [item.event_count for item in rows] == [1, 1, 1]


def test_mitigation_boundary_detects_recurrence_once_per_cycle():
    store = RiskPatternStore()
    pattern = store.ingest(_event("before", 1))
    assert pattern is not None

    store.mark_mitigated(
        pattern.pattern_id,
        at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        mitigation_id="block-45.74.28.0-24",
    )
    store.ingest(_event("late-old-event", 1, source_ip="45.74.28.228"))
    assert pattern.status == "mitigated"

    store.ingest(_event("after", 2, source_ip="45.74.28.229"))
    store.ingest(_event("same-recurrence", 3, source_ip="45.74.28.230"))
    assert pattern.status == "recurrent"
    assert pattern.recurrence_count == 1

    store.mark_mitigated(
        pattern.pattern_id,
        at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        mitigation_id="close-entrypoint",
    )
    store.ingest(_event("second-recurrence", 4, source_ip="45.74.28.231"))
    assert pattern.status == "recurrent"
    assert pattern.recurrence_count == 2


def test_daily_volume_produces_increasing_and_decreasing_trends():
    increasing = RiskPatternStore()
    sequence = 0
    for day, count in ((1, 1), (2, 3), (3, 8)):
        for _ in range(count):
            sequence += 1
            increasing.ingest(_event(f"inc-{sequence}", day))
    assert increasing.list_patterns()[0].trend == "increasing"

    decreasing = RiskPatternStore()
    sequence = 0
    for day, count in ((1, 8), (2, 3), (3, 1)):
        for _ in range(count):
            sequence += 1
            decreasing.ingest(_event(f"dec-{sequence}", day))
    assert decreasing.list_patterns()[0].trend == "decreasing"


def test_clickhouse_mapping_is_pure_and_classifies_attack_rows():
    row = {
        "event_ts": "2026-08-22 10:01:02.123",
        "device_key": "DAHUA_FORTIGATE",
        "srcip": "45.74.28.226",
        "dstip": "192.168.1.1",
        "dstport": 443,
        "proto": 6,
        "action": "login",
        "status": "failed",
        "service": "HTTPS",
        "type": "event",
        "subtype": "system",
        "srcname": "mike",
    }
    original = dict(row)

    first = risk_event_from_clickhouse_row(row)
    second = risk_event_from_clickhouse_row(row)

    assert row == original
    assert first == second
    assert first is not None
    assert first.risk_type == "credential_attack"
    assert first.scope_key == "DAHUA_FORTIGATE"
    assert first.target_account == "mike"
    assert first.target_service == "https"
    assert first.source_network == "45.74.28.0/24"
    assert first.event_id.startswith("ch-")


def test_normalized_admin_event_keeps_attempted_account_dimension():
    event = risk_event_from_clickhouse_row(
        {
            "event_ts": "2026-08-22 10:01:02.123",
            "event_id": "security-1",
            "risk_type": "admin_login_failed",
            "device_key": "edge-fw",
            "dstip": "192.168.1.1",
            "user": "mike",
            "provenance": "real",
        },
        source_table="autopoiesis.security_events",
    )

    assert event is not None
    assert event.risk_type == "admin_login_failed"
    assert event.target_account == "mike"


def test_clickhouse_mapping_distinguishes_replay_and_drill_and_ignores_normal_flow():
    base = {
        "event_ts": "2026-08-22T10:01:02Z",
        "event_id": "event-1",
        "device_key": "edge-fw",
        "srcip": "77.91.118.34",
        "dstip": "192.168.16.56",
        "dstport": 37777,
        "action": "deny",
    }

    replay = risk_event_from_clickhouse_row({**base, "replay": True})
    drill = risk_event_from_clickhouse_row({**base, "source_kind": "exercise"})
    observed = risk_event_from_clickhouse_row({**base, "replay": "false"})
    healthy = risk_event_from_clickhouse_row(
        {**base, "event_id": "ok", "action": "accept", "dstport": 443}
    )

    assert replay is not None and replay.provenance == "replay"
    assert drill is not None and drill.provenance == "drill"
    assert observed is not None and observed.provenance == "real"
    assert replay.risk_type == "exposed_service_probe"
    assert healthy is None


def test_store_can_ingest_clickhouse_query_batches_directly():
    rows = [
        {
            "event_ts": "2026-08-22T10:01:02Z",
            "event_id": "denied",
            "device_key": "edge-fw",
            "srcip": "192.168.16.56",
            "dstip": "192.168.16.255",
            "dstport": 137,
            "action": "deny",
        },
        {
            "event_ts": "2026-08-22T10:01:03Z",
            "event_id": "accepted",
            "device_key": "edge-fw",
            "srcip": "192.168.16.56",
            "dstip": "208.91.112.61",
            "dstport": 443,
            "action": "accept",
        },
    ]
    store = RiskPatternStore()

    affected = store.ingest_clickhouse_rows(rows, source_table="autopoiesis.facts_7d")

    assert len(affected) == 1
    assert affected[0].event_count == 1
    assert store.get(affected[0].pattern_id) is affected[0]
    assert affected[0].evidence_query_ranges[0].source_table == "autopoiesis.facts_7d"


def test_snapshot_round_trip_is_complete_deterministic_and_keeps_dedupe_state():
    store = RiskPatternStore(retention=timedelta(days=30), max_patterns=8)
    event = _event("one", 1)
    pattern = store.ingest(event)
    assert pattern is not None
    store.mark_mitigated(
        pattern.pattern_id,
        at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        mitigation_id="m-1",
    )
    store.ingest(_event("two", 2, account="admin"))

    snapshot = store.snapshot()
    encoded = store.snapshot_json()
    restored = RiskPatternStore.from_snapshot(json.loads(encoded))

    assert restored.snapshot() == snapshot
    assert restored.snapshot_json() == encoded
    restored.ingest(event)
    assert restored.list_patterns()[0].event_count == 2


def test_evidence_range_and_search_are_usable_by_gateway_consumers():
    store = RiskPatternStore()
    store.ingest(_event("login", source_ip="45.74.28.226", account="mike"))
    store.ingest(
        _event(
            "deny",
            source_ip="192.168.16.56",
            account="",
            service="udp/137",
            risk_type="policy_deny_activity",
        )
    )

    matched = store.search("mike 45.74.28")
    assert len(matched) == 1
    query_range = matched[0].evidence_query_ranges[0].to_dict()
    assert query_range["source_table"] == "autopoiesis.facts"
    assert query_range["start_at"] == query_range["end_at"]
    assert query_range["filters"]["risk_type"] == "credential_attack"
    assert query_range["sample_refs"] == ["autopoiesis.facts:login"]
    assert store.list_patterns(provenance="real", limit=1)[0] in store.list_patterns()


def test_stale_patterns_expire_and_pattern_count_is_bounded_deterministically():
    store = RiskPatternStore(retention=timedelta(days=2), max_patterns=2)
    old = store.ingest(_event("old", 1, scope="scope-a"))
    middle = store.ingest(_event("middle", 2, scope="scope-b"))
    newest = store.ingest(_event("new", 3, scope="scope-c"))
    assert old is not None and middle is not None and newest is not None

    assert len(store.list_patterns()) == 2
    assert {item.scope_key for item in store.list_patterns()} == {"scope-b", "scope-c"}

    expired = store.expire(datetime(2026, 8, 6, 10, 0, 1, tzinfo=UTC))
    assert len(expired) == 2
    assert store.list_patterns() == []


def test_dimension_and_dedupe_state_have_hard_bounds():
    store = RiskPatternStore(
        max_dimension_values=2,
        max_evidence_refs=2,
        max_event_ids_per_pattern=2,
    )
    for index in range(4):
        store.ingest(
            _event(
                f"event-{index}",
                1,
                source_ip=f"45.74.{index}.1",
                account=f"user-{index}",
                asset=f"192.168.1.{index + 1}",
            )
        )

    pattern = store.list_patterns()[0]
    state = store.snapshot()["patterns"][0]
    assert len(pattern.source_ips) == 2
    assert len(pattern.target_accounts) == 2
    assert len(pattern.target_assets) == 2
    assert len(state["evidence_refs"]) == 2
    assert len(state["event_ids"]) == 2
    assert pattern.dimensions_truncated is True
    assert pattern.dedupe_truncated is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention": timedelta(0)},
        {"max_patterns": 0},
        {"max_dimension_values": 0},
        {"max_evidence_refs": 0},
        {"max_event_ids_per_pattern": 0},
    ],
)
def test_invalid_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RiskPatternStore(**kwargs)
