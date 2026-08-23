"""从 FortiGate 事件流维护可复核的七天设备画像。"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import statistics
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Mapping, Protocol


DEFAULT_WINDOW_DAYS = 7
DEFAULT_PEER_TOP_K = 64
DEFAULT_PORT_TOP_K = 32
DEFAULT_INTERFACE_TOP_K = 32
DEFAULT_SESSION_MULTIPLIER = 5.0
DEFAULT_GROUP_LIMIT = 256
DEFAULT_GROUP_DEVICE_LIMIT = 512
DEFAULT_GROUP_MIN_SAMPLES = 5
DEFAULT_PEER_OUTLIER_MULTIPLIER = 8.0
DEFAULT_VOLUME_OUTLIER_MULTIPLIER = 10.0

# 自比只能发现行为变化，无法发现长期稳定的畸形行为。真数据里的 192.168.16.56
# 七天有 641 万会话却只有 2 个对端，它相对自己的历史很稳定，但会话/对端比仍是
# 同网段第二名的 8.6 倍。群体判据因此采用中位数：至少 5 台同子网设备形成基线，
# 会话/对端比超过中位数 8 倍表示接近一个数量级的集中发送；会话量超过中位数 10 倍
# 表示完整数量级的流量差距。两条阈值都保留原始计数、倍数和样本量，便于报告复核。


try:
    from .fortigate_stream import FortiEvent
except ImportError:
    # 并行开发时解析模块可能尚未落盘。结构协议让画像仍可独立测试，同时不会在这里
    # 复制解析器；解析模块一旦存在，上面的真实事件类型会自然接管。
    class FortiEvent(Protocol):
        at: datetime
        logid: str
        type: str
        subtype: str
        level: str
        action: str | None
        src_ip: str | None
        dst_ip: str | None
        src_port: int | None
        dst_port: int | None
        proto: int | None
        src_intf: str | None
        dst_intf: str | None
        user: str | None
        logdesc: str | None
        msg: str | None
        status: str | None
        sent_bytes: int | None
        rcvd_bytes: int | None
        raw: Mapping[str, str]


@dataclass
class DeviceProfile:
    ip: str
    first_seen: datetime
    last_seen: datetime
    peers: Counter[str]
    ports: Counter[int]
    interfaces: Counter[str]
    accepted: int
    denied: int
    sent_bytes: int
    rcvd_bytes: int
    hourly: dict[str, int]


Number = int | float
CriterionValue = int | float | str


@dataclass(frozen=True, slots=True)
class Anomaly:
    type: str
    explanation: str
    numbers: dict[str, Number]
    criterion: dict[str, CriterionValue]


@dataclass(slots=True)
class _HourBucket:
    hour: datetime
    first_seen: datetime
    last_seen: datetime
    sessions: int = 0
    peers: Counter[str] = field(default_factory=Counter)
    ports: Counter[int] = field(default_factory=Counter)
    interfaces: Counter[str] = field(default_factory=Counter)
    accepted: int = 0
    denied: int = 0
    sent_bytes: int = 0
    rcvd_bytes: int = 0
    peer_seen: bytearray = field(default_factory=bytearray)
    interface_seen: bytearray = field(default_factory=bytearray)


@dataclass(slots=True)
class _GroupDeviceStats:
    sessions: int = 0
    peer_count: int = 0
    accepted: int = 0
    denied: int = 0
    peer_seen: bytearray = field(default_factory=bytearray)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("FortiEvent.at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _hour_start(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _hour_label(value: datetime) -> str:
    return value.isoformat(timespec="hours")


def _display_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _bounded_increment(counter: Counter[Any], key: Any, limit: int) -> None:
    """Space-Saving 让高频新值有机会替换旧候选，同时把候选数量锁死。"""

    if key in counter:
        counter[key] += 1
        return
    if len(counter) < limit:
        counter[key] = 1
        return

    victim, count = min(counter.items(), key=lambda item: (item[1], str(item[0])))
    del counter[victim]
    # 新候选继承被替换项的误差上界。画像只把它用于 top-K 排序，异常判据使用下方的
    # 固定大小成员位图和原始会话计数，不会拿这个估计值冒充异常支撑数字。
    counter[key] = count + 1


def _positions(value: str, bit_count: int) -> tuple[int, int, int, int]:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).digest()
    first = int.from_bytes(digest[:8], "big")
    second = int.from_bytes(digest[8:], "big") | 1
    return tuple((first + index * second) % bit_count for index in range(4))  # type: ignore[return-value]


def _seen_add(bitmap: bytearray, value: str) -> None:
    bit_count = len(bitmap) * 8
    for position in _positions(value, bit_count):
        bitmap[position // 8] |= 1 << (position % 8)


def _seen_contains(bitmap: bytearray, value: str) -> bool:
    bit_count = len(bitmap) * 8
    return all(
        bitmap[position // 8] & (1 << (position % 8))
        for position in _positions(value, bit_count)
    )


class ProfileStore:
    """以有界小时桶维护设备画像，并在写入事件前判断偏离。"""

    def __init__(
        self,
        *,
        peer_top_k: int = DEFAULT_PEER_TOP_K,
        port_top_k: int = DEFAULT_PORT_TOP_K,
        interface_top_k: int = DEFAULT_INTERFACE_TOP_K,
        session_multiplier: float = DEFAULT_SESSION_MULTIPLIER,
        membership_bits: int = 8192,
        group_limit: int = DEFAULT_GROUP_LIMIT,
        group_device_limit: int = DEFAULT_GROUP_DEVICE_LIMIT,
        group_min_samples: int = DEFAULT_GROUP_MIN_SAMPLES,
        peer_outlier_multiplier: float = DEFAULT_PEER_OUTLIER_MULTIPLIER,
        volume_outlier_multiplier: float = DEFAULT_VOLUME_OUTLIER_MULTIPLIER,
    ) -> None:
        if min(peer_top_k, port_top_k, interface_top_k) <= 0:
            raise ValueError("top-K limits must be positive")
        if session_multiplier <= 0 or not math.isfinite(session_multiplier):
            raise ValueError("session_multiplier must be a positive finite number")
        if membership_bits < 64 or membership_bits % 8:
            raise ValueError("membership_bits must be a multiple of 8 and at least 64")
        if min(group_limit, group_device_limit, group_min_samples) <= 0:
            raise ValueError("group limits and minimum samples must be positive")
        if group_min_samples > group_device_limit:
            raise ValueError("group_min_samples cannot exceed group_device_limit")
        for name, value in (
            ("peer_outlier_multiplier", peer_outlier_multiplier),
            ("volume_outlier_multiplier", volume_outlier_multiplier),
        ):
            if value <= 1 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and greater than 1")

        self.peer_top_k = peer_top_k
        self.port_top_k = port_top_k
        self.interface_top_k = interface_top_k
        self.session_multiplier = float(session_multiplier)
        self.membership_bits = membership_bits
        self.group_limit = group_limit
        self.group_device_limit = group_device_limit
        self.group_min_samples = group_min_samples
        self.peer_outlier_multiplier = float(peer_outlier_multiplier)
        self.volume_outlier_multiplier = float(volume_outlier_multiplier)
        self._profiles: dict[str, dict[datetime, _HourBucket]] = {}
        self._group_baselines: OrderedDict[
            str, OrderedDict[str, _GroupDeviceStats]
        ] = OrderedDict()
        self._watermark: datetime | None = None
        self._last_auto_prune_hour: datetime | None = None
        self._lock = RLock()

    def observe(self, event: FortiEvent) -> None:
        """把一条事件计入画像；晚于七天高水位的迟到事件直接忽略。"""

        at = _utc(event.at)
        with self._lock:
            self._advance_and_prune(at, DEFAULT_WINDOW_DAYS, force=False)
            if event.src_ip is None or self._outside_window(at, DEFAULT_WINDOW_DAYS):
                return

            ip = str(event.src_ip)
            hour = _hour_start(at)
            buckets = self._profiles.setdefault(ip, {})
            peer = str(event.dst_ip) if event.dst_ip is not None else None
            bucket = buckets.get(hour)
            if bucket is None:
                bitmap_bytes = self.membership_bits // 8
                bucket = _HourBucket(
                    hour=hour,
                    first_seen=at,
                    last_seen=at,
                    peer_seen=bytearray(bitmap_bytes),
                    interface_seen=bytearray(bitmap_bytes),
                )
                buckets[hour] = bucket

            bucket.first_seen = min(bucket.first_seen, at)
            bucket.last_seen = max(bucket.last_seen, at)
            bucket.sessions += 1

            if peer is not None:
                _bounded_increment(bucket.peers, peer, self.peer_top_k)
                _seen_add(bucket.peer_seen, peer)
            if event.dst_port is not None:
                _bounded_increment(bucket.ports, int(event.dst_port), self.port_top_k)
            if event.src_intf is not None:
                interface = str(event.src_intf)
                _bounded_increment(bucket.interfaces, interface, self.interface_top_k)
                _seen_add(bucket.interface_seen, interface)

            action = (event.action or "").casefold()
            bucket.accepted += int(action == "accept")
            bucket.denied += int(action == "deny")
            bucket.sent_bytes += int(event.sent_bytes or 0)
            bucket.rcvd_bytes += int(event.rcvd_bytes or 0)
            self._record_group_observation(ip, event)

    def profile(self, ip: str) -> DeviceProfile | None:
        """返回当前窗口的快照，调用方修改 Counter 不会污染存储。"""

        with self._lock:
            buckets = self._profiles.get(str(ip))
            if not buckets:
                return None
            values = list(buckets.values())
            peers: Counter[str] = Counter()
            ports: Counter[int] = Counter()
            interfaces: Counter[str] = Counter()
            hourly: dict[str, int] = {}
            for bucket in values:
                peers.update(bucket.peers)
                ports.update(bucket.ports)
                interfaces.update(bucket.interfaces)
                hourly[_hour_label(bucket.hour)] = bucket.sessions

            # 各小时的候选合并后还要再截一次，否则 168 个桶的候选并集会随时间放大。
            peers = Counter(dict(peers.most_common(self.peer_top_k)))
            ports = Counter(dict(ports.most_common(self.port_top_k)))
            interfaces = Counter(dict(interfaces.most_common(self.interface_top_k)))
            return DeviceProfile(
                ip=str(ip),
                first_seen=min(bucket.first_seen for bucket in values),
                last_seen=max(bucket.last_seen for bucket in values),
                peers=peers,
                ports=ports,
                interfaces=interfaces,
                accepted=sum(bucket.accepted for bucket in values),
                denied=sum(bucket.denied for bucket in values),
                sent_bytes=sum(bucket.sent_bytes for bucket in values),
                rcvd_bytes=sum(bucket.rcvd_bytes for bucket in values),
                hourly=dict(sorted(hourly.items())),
            )

    def seed_group_summary(
        self,
        ip: str,
        *,
        sessions: int,
        peer_count: int,
        accepted: int = 0,
        denied: int = 0,
        known_peers: tuple[str, ...] = (),
    ) -> None:
        """Load an exact cohort aggregate without replaying every historical flow."""
        if min(sessions, peer_count, accepted, denied) < 0:
            raise ValueError("group summary counts cannot be negative")
        bitmap = bytearray(self.membership_bits // 8)
        for peer in known_peers:
            _seen_add(bitmap, peer)
        with self._lock:
            self._insert_group_device(
                str(ip),
                _GroupDeviceStats(
                    sessions=int(sessions),
                    peer_count=int(peer_count),
                    accepted=int(accepted),
                    denied=int(denied),
                    peer_seen=bitmap,
                ),
            )

    def anomalies(self, event: FortiEvent) -> list[Anomaly]:
        """在当前事件写入前给出异常；此方法不会把事件计入画像。"""

        at = _utc(event.at)
        with self._lock:
            self._advance_and_prune(at, DEFAULT_WINDOW_DAYS, force=False)
            if event.src_ip is None or self._outside_window(at, DEFAULT_WINDOW_DAYS):
                return []

            ip = str(event.src_ip)
            buckets = self._profiles.get(ip, {})
            result: list[Anomaly] = []
            criterion_base: dict[str, CriterionValue] = {
                "window_days": DEFAULT_WINDOW_DAYS,
            }

            if event.dst_ip is not None:
                peer = str(event.dst_ip)
                if not self._was_seen(buckets.values(), peer, peer=True):
                    result.append(
                        Anomaly(
                            type="new_peer",
                            explanation=f"{ip} 第一次和 {peer} 通信",
                            numbers={"previous_sessions": 0, "current_sessions": 1},
                            criterion={
                                **criterion_base,
                                "previous_sessions_must_equal": 0,
                            },
                        )
                    )

            action = (event.action or "").casefold()
            previous_denied = sum(bucket.denied for bucket in buckets.values())
            if action == "deny" and previous_denied == 0:
                result.append(
                    Anomaly(
                        type="first_deny",
                        explanation=f"{ip} 过去 7 天第一次被 deny",
                        numbers={"previous_denied": 0, "current_denied": 1},
                        criterion={
                            **criterion_base,
                            "previous_denied_must_equal": 0,
                        },
                    )
                )

            if event.src_intf is not None:
                interface = str(event.src_intf)
                if not self._was_seen(buckets.values(), interface, peer=False):
                    result.append(
                        Anomaly(
                            type="new_interface",
                            explanation=f"{ip} 过去 7 天第一次出现在接口 {interface} 上",
                            numbers={"previous_occurrences": 0, "current_occurrences": 1},
                            criterion={
                                **criterion_base,
                                "previous_occurrences_must_equal": 0,
                            },
                        )
                    )

            current_hour = _hour_start(at)
            historical = [
                bucket.sessions
                for hour, bucket in buckets.items()
                if hour < current_hour and hour.hour == current_hour.hour
            ]
            current_sessions = buckets.get(current_hour, _EMPTY_BUCKET).sessions + 1
            if historical:
                baseline = float(statistics.median(historical))
                threshold = baseline * self.session_multiplier
                # 中位数为零时“几倍”没有可复核的有限含义，留给首次时段规则处理更清楚。
                if baseline > 0 and current_sessions > threshold:
                    multiple = current_sessions / baseline
                    result.append(
                        Anomaly(
                            type="session_spike",
                            explanation=(
                                f"{ip} 今天 {current_hour.hour} 点的会话数是它平时这个点的 "
                                f"{_display_number(multiple)} 倍"
                            ),
                            numbers={
                                "current_hour_sessions": current_sessions,
                                "baseline_median": baseline,
                                "multiple": multiple,
                                "baseline_samples": len(historical),
                            },
                            criterion={
                                **criterion_base,
                                "same_hour_utc": current_hour.hour,
                                "multiplier": self.session_multiplier,
                                "threshold_sessions": threshold,
                            },
                        )
                    )

            result.extend(self._group_anomalies(ip, event))
            return result

    def prune(self, *, now: datetime, window_days: int = DEFAULT_WINDOW_DAYS) -> int:
        """淘汰窗口外小时桶，返回从画像中删除的事件数。"""

        if window_days <= 0:
            raise ValueError("window_days must be positive")
        with self._lock:
            return self._advance_and_prune(_utc(now), window_days, force=True)

    def to_dict(self) -> dict[str, Any]:
        """生成只含 JSON 基础类型的完整快照。"""

        with self._lock:
            devices: dict[str, list[dict[str, Any]]] = {}
            for ip, buckets in sorted(self._profiles.items()):
                devices[ip] = [
                    self._bucket_to_dict(bucket)
                    for _hour, bucket in sorted(buckets.items())
                ]
            return {
                "version": 2,
                "config": {
                    "peer_top_k": self.peer_top_k,
                    "port_top_k": self.port_top_k,
                    "interface_top_k": self.interface_top_k,
                    "session_multiplier": self.session_multiplier,
                    "membership_bits": self.membership_bits,
                    "group_limit": self.group_limit,
                    "group_device_limit": self.group_device_limit,
                    "group_min_samples": self.group_min_samples,
                    "peer_outlier_multiplier": self.peer_outlier_multiplier,
                    "volume_outlier_multiplier": self.volume_outlier_multiplier,
                },
                "watermark": self._watermark.isoformat() if self._watermark else None,
                "devices": devices,
                "group_baselines": [
                    {
                        "group": group,
                        "devices": [
                            {
                                "ip": ip,
                                "sessions": stats.sessions,
                                "peer_count": stats.peer_count,
                                "accepted": stats.accepted,
                                "denied": stats.denied,
                                "peer_seen": base64.b64encode(stats.peer_seen).decode(
                                    "ascii"
                                ),
                            }
                            for ip, stats in members.items()
                        ],
                    }
                    for group, members in self._group_baselines.items()
                ],
            }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProfileStore:
        version = payload.get("version")
        if version not in (1, 2):
            raise ValueError("unsupported profile snapshot version")
        config = payload.get("config")
        devices = payload.get("devices")
        if not isinstance(config, Mapping) or not isinstance(devices, Mapping):
            raise ValueError("invalid profile snapshot")

        store = cls(
            peer_top_k=int(config["peer_top_k"]),
            port_top_k=int(config["port_top_k"]),
            interface_top_k=int(config["interface_top_k"]),
            session_multiplier=float(config["session_multiplier"]),
            membership_bits=int(config["membership_bits"]),
            group_limit=int(config.get("group_limit", DEFAULT_GROUP_LIMIT)),
            group_device_limit=int(
                config.get("group_device_limit", DEFAULT_GROUP_DEVICE_LIMIT)
            ),
            group_min_samples=int(
                config.get("group_min_samples", DEFAULT_GROUP_MIN_SAMPLES)
            ),
            peer_outlier_multiplier=float(
                config.get(
                    "peer_outlier_multiplier", DEFAULT_PEER_OUTLIER_MULTIPLIER
                )
            ),
            volume_outlier_multiplier=float(
                config.get(
                    "volume_outlier_multiplier", DEFAULT_VOLUME_OUTLIER_MULTIPLIER
                )
            ),
        )
        watermark = payload.get("watermark")
        store._watermark = _utc(datetime.fromisoformat(watermark)) if watermark else None
        for raw_ip, raw_buckets in devices.items():
            if not isinstance(raw_buckets, list):
                raise ValueError("invalid bucket list in profile snapshot")
            ip = str(raw_ip)
            store._profiles[ip] = {}
            for raw_bucket in raw_buckets:
                bucket = store._bucket_from_dict(raw_bucket)
                store._profiles[ip][bucket.hour] = bucket
            if not store._profiles[ip]:
                del store._profiles[ip]
        if version == 2:
            raw_groups = payload.get("group_baselines")
            if not isinstance(raw_groups, list):
                raise ValueError("invalid group baselines in profile snapshot")
            for raw_group in raw_groups:
                if not isinstance(raw_group, Mapping):
                    raise ValueError("invalid group baseline in profile snapshot")
                group = str(raw_group["group"])
                raw_members = raw_group.get("devices")
                if not isinstance(raw_members, list):
                    raise ValueError("invalid group devices in profile snapshot")
                members: OrderedDict[str, _GroupDeviceStats] = OrderedDict()
                for raw_stats in raw_members:
                    if not isinstance(raw_stats, Mapping):
                        raise ValueError("invalid group device stats in profile snapshot")
                    peer_seen = bytearray(
                        base64.b64decode(raw_stats["peer_seen"], validate=True)
                    )
                    if len(peer_seen) != store.membership_bits // 8:
                        raise ValueError(
                            "group membership bitmap size does not match snapshot config"
                        )
                    members[str(raw_stats["ip"])] = _GroupDeviceStats(
                        sessions=int(raw_stats["sessions"]),
                        peer_count=int(raw_stats["peer_count"]),
                        accepted=int(raw_stats["accepted"]),
                        denied=int(raw_stats["denied"]),
                        peer_seen=peer_seen,
                    )
                if len(members) > store.group_device_limit:
                    raise ValueError("group baseline exceeds configured device limit")
                store._group_baselines[group] = members
            if len(store._group_baselines) > store.group_limit:
                raise ValueError("group baselines exceed configured group limit")
        else:
            # 第一版快照没有群体汇总。加载时从现有七天桶重建，旧数据可直接升级。
            for ip in store._profiles:
                store._insert_group_device(ip, store._summarize_device(ip))
        return store

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def loads(cls, payload: str | bytes | bytearray) -> ProfileStore:
        return cls.from_dict(json.loads(payload))

    # 这两个别名让持久化调用处直接表达意图，同时仍保留便于检查的 to_dict/from_dict。
    serialize = to_dict
    deserialize = from_dict
    to_json = dumps
    from_json = loads

    def _outside_window(self, at: datetime, window_days: int) -> bool:
        if self._watermark is None:
            return False
        return at <= self._watermark - timedelta(days=window_days)

    def _advance_and_prune(self, now: datetime, window_days: int, *, force: bool) -> int:
        if self._watermark is None or now > self._watermark:
            self._watermark = now
        effective_now = self._watermark
        prune_hour = _hour_start(effective_now)
        # 时间桶只有到小时切换后才会增加新的过期候选。把全设备扫描限制为每小时一次，
        # 可以避免每秒三条的流量把同一批 168 个桶反复检查几十万次。
        if (
            not force
            and self._last_auto_prune_hour is not None
            and prune_hour <= self._last_auto_prune_hour
        ):
            return 0
        self._last_auto_prune_hour = prune_hour
        cutoff = effective_now - timedelta(days=window_days)
        removed = 0
        empty_devices: list[str] = []
        changed_devices: list[str] = []
        for ip, buckets in self._profiles.items():
            expired = [hour for hour, bucket in buckets.items() if bucket.last_seen <= cutoff]
            for hour in expired:
                removed += buckets[hour].sessions
                del buckets[hour]
            if expired:
                changed_devices.append(ip)
            if not buckets:
                empty_devices.append(ip)
        for ip in empty_devices:
            del self._profiles[ip]
            self._remove_group_device(ip)
        for ip in changed_devices:
            if ip not in empty_devices:
                self._refresh_tracked_group_device(ip)
        return removed

    @staticmethod
    def _group_for_ip(ip: str) -> str | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        prefix = 24 if address.version == 4 else 64
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))

    def _record_group_observation(self, ip: str, event: FortiEvent) -> None:
        group = self._group_for_ip(ip)
        if group is None:
            return
        members = self._group_baselines.get(group)
        stats = members.get(ip) if members is not None else None
        if stats is None:
            self._insert_group_device(ip, self._summarize_device(ip))
            return

        stats.sessions += 1
        if event.dst_ip is not None:
            peer = str(event.dst_ip)
            if not _seen_contains(stats.peer_seen, peer):
                stats.peer_count += 1
                _seen_add(stats.peer_seen, peer)
        action = (event.action or "").casefold()
        stats.accepted += int(action == "accept")
        stats.denied += int(action == "deny")
        members.move_to_end(ip)
        self._group_baselines.move_to_end(group)

    def _summarize_device(self, ip: str) -> _GroupDeviceStats:
        buckets = self._profiles.get(ip, {})
        peer_candidates = {
            peer for bucket in buckets.values() for peer in bucket.peers.keys()
        }
        bitmap_bytes = self.membership_bits // 8
        peer_seen_bits = 0
        for bucket in buckets.values():
            peer_seen_bits |= int.from_bytes(bucket.peer_seen, "little")
        peer_seen = bytearray(peer_seen_bits.to_bytes(bitmap_bytes, "little"))
        return _GroupDeviceStats(
            sessions=sum(bucket.sessions for bucket in buckets.values()),
            peer_count=len(peer_candidates),
            accepted=sum(bucket.accepted for bucket in buckets.values()),
            denied=sum(bucket.denied for bucket in buckets.values()),
            peer_seen=peer_seen,
        )

    def _insert_group_device(self, ip: str, stats: _GroupDeviceStats) -> None:
        group = self._group_for_ip(ip)
        if group is None:
            return
        members = self._group_baselines.get(group)
        if members is None:
            if len(self._group_baselines) >= self.group_limit:
                self._group_baselines.popitem(last=False)
            members = OrderedDict()
            self._group_baselines[group] = members
        if ip not in members and len(members) >= self.group_device_limit:
            members.popitem(last=False)
        members[ip] = stats
        members.move_to_end(ip)
        self._group_baselines.move_to_end(group)

    def _remove_group_device(self, ip: str) -> None:
        group = self._group_for_ip(ip)
        members = self._group_baselines.get(group) if group is not None else None
        if members is None:
            return
        members.pop(ip, None)
        if not members:
            del self._group_baselines[group]

    def _refresh_tracked_group_device(self, ip: str) -> None:
        group = self._group_for_ip(ip)
        members = self._group_baselines.get(group) if group is not None else None
        if members is not None and ip in members:
            members[ip] = self._summarize_device(ip)

    def _group_anomalies(
        self,
        ip: str,
        event: FortiEvent,
    ) -> list[Anomaly]:
        group = self._group_for_ip(ip)
        members = self._group_baselines.get(group) if group is not None else None
        if group is None or members is None:
            return []

        stored = members.get(ip)
        current = (
            _GroupDeviceStats(
                sessions=stored.sessions,
                peer_count=stored.peer_count,
                accepted=stored.accepted,
                denied=stored.denied,
                peer_seen=bytearray(stored.peer_seen),
            )
            if stored is not None
            else self._summarize_device(ip)
        )
        current.sessions += 1
        if event.dst_ip is not None:
            peer = str(event.dst_ip)
            if not _seen_contains(current.peer_seen, peer):
                current.peer_count += 1
                _seen_add(current.peer_seen, peer)
        action = (event.action or "").casefold()
        current.accepted += int(action == "accept")
        current.denied += int(action == "deny")

        controls = [stats for other_ip, stats in members.items() if other_ip != ip]
        result: list[Anomaly] = []

        ratio_controls = [
            stats.sessions / stats.peer_count
            for stats in controls
            if stats.peer_count > 0
        ]
        if current.peer_count > 0 and len(ratio_controls) >= self.group_min_samples:
            median_ratio = float(statistics.median(ratio_controls))
            current_ratio = current.sessions / current.peer_count
            threshold = median_ratio * self.peer_outlier_multiplier
            if median_ratio > 0 and current_ratio > threshold:
                multiple = current_ratio / median_ratio
                result.append(
                    Anomaly(
                        type="peer_outlier",
                        explanation=(
                            f"{ip} 过去 7 天 {current.sessions} 次会话只涉及 "
                            f"{current.peer_count} 个对端，每个对端 "
                            f"{_display_number(current_ratio)} 次，是同子网中位数 "
                            f"{_display_number(median_ratio)} 次的 "
                            f"{_display_number(multiple)} 倍"
                        ),
                        numbers={
                            "sessions": current.sessions,
                            "peer_count": current.peer_count,
                            "sessions_per_peer": current_ratio,
                            "group_median_sessions_per_peer": median_ratio,
                            "multiple": multiple,
                        },
                        criterion={
                            "window_days": DEFAULT_WINDOW_DAYS,
                            "group": group,
                            "group_samples": len(ratio_controls),
                            "multiplier": self.peer_outlier_multiplier,
                            "threshold_sessions_per_peer": threshold,
                        },
                    )
                )

        session_controls = [stats.sessions for stats in controls]
        if len(session_controls) >= self.group_min_samples:
            median_sessions = float(statistics.median(session_controls))
            threshold = median_sessions * self.volume_outlier_multiplier
            if median_sessions > 0 and current.sessions > threshold:
                multiple = current.sessions / median_sessions
                result.append(
                    Anomaly(
                        type="volume_outlier",
                        explanation=(
                            f"{ip} 过去 7 天有 {current.sessions} 次会话，是同子网设备"
                            f"中位数 {_display_number(median_sessions)} 次的 "
                            f"{_display_number(multiple)} 倍"
                        ),
                        numbers={
                            "sessions": current.sessions,
                            "group_median_sessions": median_sessions,
                            "multiple": multiple,
                        },
                        criterion={
                            "window_days": DEFAULT_WINDOW_DAYS,
                            "group": group,
                            "group_samples": len(session_controls),
                            "multiplier": self.volume_outlier_multiplier,
                            "threshold_sessions": threshold,
                        },
                    )
                )
        return result

    @staticmethod
    def _was_seen(buckets: Any, value: str, *, peer: bool) -> bool:
        for bucket in buckets:
            bitmap = bucket.peer_seen if peer else bucket.interface_seen
            if _seen_contains(bitmap, value):
                return True
        return False

    @staticmethod
    def _bucket_to_dict(bucket: _HourBucket) -> dict[str, Any]:
        return {
            "hour": bucket.hour.isoformat(),
            "first_seen": bucket.first_seen.isoformat(),
            "last_seen": bucket.last_seen.isoformat(),
            "sessions": bucket.sessions,
            "peers": dict(bucket.peers),
            "ports": {str(key): value for key, value in bucket.ports.items()},
            "interfaces": dict(bucket.interfaces),
            "accepted": bucket.accepted,
            "denied": bucket.denied,
            "sent_bytes": bucket.sent_bytes,
            "rcvd_bytes": bucket.rcvd_bytes,
            "peer_seen": base64.b64encode(bucket.peer_seen).decode("ascii"),
            "interface_seen": base64.b64encode(bucket.interface_seen).decode("ascii"),
        }

    def _bucket_from_dict(self, raw: Mapping[str, Any]) -> _HourBucket:
        peer_seen = bytearray(base64.b64decode(raw["peer_seen"], validate=True))
        interface_seen = bytearray(base64.b64decode(raw["interface_seen"], validate=True))
        expected_bytes = self.membership_bits // 8
        if len(peer_seen) != expected_bytes or len(interface_seen) != expected_bytes:
            raise ValueError("membership bitmap size does not match snapshot config")
        return _HourBucket(
            hour=_utc(datetime.fromisoformat(raw["hour"])),
            first_seen=_utc(datetime.fromisoformat(raw["first_seen"])),
            last_seen=_utc(datetime.fromisoformat(raw["last_seen"])),
            sessions=int(raw["sessions"]),
            peers=Counter({str(key): int(value) for key, value in raw["peers"].items()}),
            ports=Counter({int(key): int(value) for key, value in raw["ports"].items()}),
            interfaces=Counter(
                {str(key): int(value) for key, value in raw["interfaces"].items()}
            ),
            accepted=int(raw["accepted"]),
            denied=int(raw["denied"]),
            sent_bytes=int(raw["sent_bytes"]),
            rcvd_bytes=int(raw["rcvd_bytes"]),
            peer_seen=peer_seen,
            interface_seen=interface_seen,
        )


# 查询当前小时不存在时复用一个零值，避免仅做异常判断就向画像写入空桶。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EMPTY_BUCKET = _HourBucket(hour=_EPOCH, first_seen=_EPOCH, last_seen=_EPOCH)


__all__ = ["Anomaly", "DeviceProfile", "FortiEvent", "ProfileStore"]
