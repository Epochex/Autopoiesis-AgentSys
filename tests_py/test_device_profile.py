"""设备画像的数字必须能从合成事件逐条数回来。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from domains.network_rca.device_profile import ProfileStore


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class Event:
    at: datetime
    logid: str = "0001000014"
    type: str = "traffic"
    subtype: str = "forward"
    level: str = "notice"
    action: str | None = "accept"
    src_ip: str | None = "192.168.16.42"
    dst_ip: str | None = "1.1.1.1"
    src_port: int | None = 40000
    dst_port: int | None = 443
    proto: int | None = 6
    src_intf: str | None = "port5"
    dst_intf: str | None = "wan1"
    user: str | None = None
    logdesc: str | None = None
    msg: str | None = None
    status: str | None = None
    sent_bytes: int | None = 100
    rcvd_bytes: int | None = 200
    raw: Mapping[str, str] = field(default_factory=dict)


def event(at: datetime, **changes: object) -> Event:
    values = {name: getattr(Event(at), name) for name in Event.__dataclass_fields__}
    values.update(changes)
    return Event(**values)


def anomaly_of(store: ProfileStore, item: Event, kind: str):
    return next(anomaly for anomaly in store.anomalies(item) if anomaly.type == kind)


def test_synthetic_stream_builds_reproducible_profile() -> None:
    store = ProfileStore()
    start = datetime(2026, 8, 20, 15, 5, tzinfo=UTC)
    stream = [
        event(start, dst_ip="8.8.8.8", dst_port=53, sent_bytes=10, rcvd_bytes=20),
        event(start + timedelta(minutes=10), dst_ip="8.8.8.8", dst_port=53),
        event(
            start + timedelta(hours=1),
            dst_ip="1.1.1.1",
            dst_port=443,
            action="deny",
            src_intf="port6",
            sent_bytes=30,
            rcvd_bytes=40,
        ),
    ]
    for item in stream:
        store.observe(item)

    profile = store.profile("192.168.16.42")
    assert profile is not None
    assert profile.first_seen == start
    assert profile.last_seen == start + timedelta(hours=1)
    assert profile.peers == Counter({"8.8.8.8": 2, "1.1.1.1": 1})
    assert profile.ports == Counter({53: 2, 443: 1})
    assert profile.interfaces == Counter({"port5": 2, "port6": 1})
    assert (profile.accepted, profile.denied) == (2, 1)
    assert (profile.sent_bytes, profile.rcvd_bytes) == (140, 260)
    assert profile.hourly == {
        "2026-08-20T15+00:00": 2,
        "2026-08-20T16+00:00": 1,
    }


def test_peer_and_port_candidates_stay_at_top_k() -> None:
    store = ProfileStore(peer_top_k=3, port_top_k=2, interface_top_k=2)
    start = datetime(2026, 8, 20, 12, tzinfo=UTC)
    for index in range(100):
        store.observe(
            event(
                start + timedelta(seconds=index),
                dst_ip=f"203.0.113.{index}",
                dst_port=10000 + index,
                src_intf=f"port{index}",
            )
        )

    profile = store.profile("192.168.16.42")
    assert profile is not None
    assert len(profile.peers) == 3
    assert len(profile.ports) == 2
    assert len(profile.interfaces) == 2


def test_new_peer_anomaly_contains_raw_counts_and_rule() -> None:
    store = ProfileStore()
    at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    store.observe(event(at, dst_ip="1.1.1.1"))

    new = event(at + timedelta(minutes=1), dst_ip="8.8.8.8")
    anomaly = anomaly_of(store, new, "new_peer")
    assert anomaly.explanation == "192.168.16.42 第一次和 8.8.8.8 通信"
    assert anomaly.numbers == {"previous_sessions": 0, "current_sessions": 1}
    assert anomaly.criterion == {
        "window_days": 7,
        "previous_sessions_must_equal": 0,
    }

    store.observe(new)
    assert not any(item.type == "new_peer" for item in store.anomalies(new))


def test_same_hour_spike_uses_devices_own_median() -> None:
    store = ProfileStore(session_multiplier=5)
    first = datetime(2026, 8, 16, 15, tzinfo=UTC)
    # 七个历史小时各两条，所以中位数和阈值都能直接手算为 2 和 10。
    for day in range(7):
        for minute in (5, 25):
            store.observe(event(first + timedelta(days=day, minutes=minute)))

    current = first + timedelta(days=7)
    for minute in range(10):
        store.observe(event(current + timedelta(minutes=minute)))
    item = event(current + timedelta(minutes=10))
    anomaly = anomaly_of(store, item, "session_spike")

    assert anomaly.numbers == {
        "current_hour_sessions": 11,
        "baseline_median": 2.0,
        "multiple": 5.5,
        "baseline_samples": 7,
    }
    assert anomaly.criterion == {
        "window_days": 7,
        "same_hour_utc": 15,
        "multiplier": 5.0,
        "threshold_sessions": 10.0,
    }
    assert anomaly.explanation.endswith("今天 15 点的会话数是它平时这个点的 5.5 倍")


def test_first_deny_and_first_interface_are_separately_reproducible() -> None:
    store = ProfileStore()
    at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    store.observe(event(at, dst_ip="1.1.1.1", src_intf="port5", action="accept"))
    item = event(
        at + timedelta(minutes=1),
        dst_ip="1.1.1.1",
        src_intf="port6",
        action="deny",
    )

    deny = anomaly_of(store, item, "first_deny")
    interface = anomaly_of(store, item, "new_interface")
    assert deny.numbers == {"previous_denied": 0, "current_denied": 1}
    assert deny.criterion["previous_denied_must_equal"] == 0
    assert interface.numbers == {"previous_occurrences": 0, "current_occurrences": 1}
    assert interface.criterion["window_days"] == 7


def test_fourteen_days_expire_old_evidence_and_bound_bucket_count() -> None:
    store = ProfileStore(peer_top_k=4, port_top_k=3)
    first = datetime(2026, 8, 1, 15, tzinfo=UTC)
    store.observe(
        event(first, dst_ip="8.8.8.8", src_intf="old-port", action="deny")
    )

    # 连续十四天、每小时一个新值会冲击候选集，也能证明时间桶不会随天数一直累积。
    for hour in range(14 * 24):
        at = first + timedelta(hours=hour, minutes=10)
        store.observe(
            event(
                at,
                dst_ip=f"198.51.{hour // 256}.{hour % 256}",
                dst_port=10000 + hour,
                src_intf="port5",
                action="accept",
            )
        )

    now = first + timedelta(days=14)
    candidate = event(now, dst_ip="8.8.8.8", src_intf="old-port", action="deny")
    kinds = {item.type for item in store.anomalies(candidate)}
    assert {"new_peer", "first_deny", "new_interface"} <= kinds

    profile = store.profile("192.168.16.42")
    assert profile is not None
    assert profile.first_seen > first
    assert len(profile.hourly) <= 7 * 24
    assert len(profile.peers) <= 4
    assert len(profile.ports) <= 3

    idle = ProfileStore()
    idle.observe(event(first))
    idle.observe(event(first + timedelta(minutes=1)))
    assert idle.prune(now=now) == 2
    assert idle.profile("192.168.16.42") is None


def test_serialization_round_trip_preserves_profile_and_next_decision() -> None:
    store = ProfileStore(peer_top_k=5, session_multiplier=4)
    at = datetime(2026, 8, 20, 9, tzinfo=UTC)
    for offset in range(3):
        store.observe(
            event(
                at + timedelta(minutes=offset),
                dst_ip="8.8.8.8",
                dst_port=53,
                sent_bytes=offset,
            )
        )

    restored = ProfileStore.loads(store.dumps())
    assert restored.profile("192.168.16.42") == store.profile("192.168.16.42")

    next_event = event(at + timedelta(hours=1), dst_ip="9.9.9.9", src_intf="port6")
    assert restored.anomalies(next_event) == store.anomalies(next_event)
    assert restored.to_dict() == store.to_dict()


def test_stable_self_history_can_still_be_an_outlier_in_its_subnet() -> None:
    store = ProfileStore()
    first = datetime(2026, 8, 18, 10, tzinfo=UTC)

    # 五台同网段设备各有 40 次会话、5 个对端，两个群体中位数分别是 40 和 8。
    for device in range(5):
        src_ip = f"192.168.16.{10 + device}"
        for index in range(40):
            store.observe(
                event(
                    first + timedelta(seconds=index),
                    src_ip=src_ip,
                    dst_ip=f"203.0.113.{index % 5 + 1}",
                )
            )

    # 仿照 192.168.16.56：每天都稳定地产生大量会话，只访问两个对端，99% deny。
    target_ip = "192.168.16.56"
    for day in range(2):
        for index in range(200):
            store.observe(
                event(
                    first + timedelta(days=day, seconds=index),
                    src_ip=target_ip,
                    dst_ip=f"198.51.100.{index % 2 + 1}",
                    action="accept" if index % 100 == 0 else "deny",
                )
            )

    candidate = event(
        first + timedelta(days=2),
        src_ip=target_ip,
        dst_ip="198.51.100.1",
        action="deny",
    )
    anomalies = store.anomalies(candidate)
    kinds = {item.type for item in anomalies}

    # 对端、deny、接口和同一小时流量都延续自己的历史，所以原有自比判据全部沉默。
    assert not {"new_peer", "first_deny", "new_interface", "session_spike"} & kinds
    assert {"peer_outlier", "volume_outlier"} <= kinds

    peer = next(item for item in anomalies if item.type == "peer_outlier")
    assert peer.numbers == {
        "sessions": 401,
        "peer_count": 2,
        "sessions_per_peer": 200.5,
        "group_median_sessions_per_peer": 8.0,
        "multiple": 25.0625,
    }
    assert peer.criterion == {
        "window_days": 7,
        "group": "192.168.16.0/24",
        "group_samples": 5,
        "multiplier": 8.0,
        "threshold_sessions_per_peer": 64.0,
    }
    assert peer.explanation == (
        "192.168.16.56 过去 7 天 401 次会话只涉及 2 个对端，每个对端 "
        "200.5 次，是同子网中位数 8 次的 25.1 倍"
    )

    volume = next(item for item in anomalies if item.type == "volume_outlier")
    assert volume.numbers == {
        "sessions": 401,
        "group_median_sessions": 40.0,
        "multiple": 10.025,
    }
    assert volume.criterion == {
        "window_days": 7,
        "group": "192.168.16.0/24",
        "group_samples": 5,
        "multiplier": 10.0,
        "threshold_sessions": 400.0,
    }

    restored = ProfileStore.loads(store.dumps())
    assert restored.anomalies(candidate) == anomalies


def test_group_baseline_has_fixed_group_and_device_limits() -> None:
    store = ProfileStore(group_limit=2, group_device_limit=4, group_min_samples=2)
    at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    for subnet in range(5):
        for device in range(10):
            store.observe(
                event(
                    at + timedelta(seconds=subnet * 10 + device),
                    src_ip=f"10.{subnet}.0.{device + 1}",
                    dst_ip="203.0.113.1",
                )
            )

    groups = store.to_dict()["group_baselines"]
    assert len(groups) == 2
    assert all(len(group["devices"]) <= 4 for group in groups)
