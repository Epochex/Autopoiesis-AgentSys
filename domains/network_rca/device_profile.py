"""从 FortiGate 事件流维护可复核的七天设备画像。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Mapping, Protocol


DEFAULT_WINDOW_DAYS = 7
DEFAULT_PEER_TOP_K = 64
DEFAULT_PORT_TOP_K = 32
DEFAULT_INTERFACE_TOP_K = 32
DEFAULT_SESSION_MULTIPLIER = 5.0


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
    ) -> None:
        if min(peer_top_k, port_top_k, interface_top_k) <= 0:
            raise ValueError("top-K limits must be positive")
        if session_multiplier <= 0 or not math.isfinite(session_multiplier):
            raise ValueError("session_multiplier must be a positive finite number")
        if membership_bits < 64 or membership_bits % 8:
            raise ValueError("membership_bits must be a multiple of 8 and at least 64")

        self.peer_top_k = peer_top_k
        self.port_top_k = port_top_k
        self.interface_top_k = interface_top_k
        self.session_multiplier = float(session_multiplier)
        self.membership_bits = membership_bits
        self._profiles: dict[str, dict[datetime, _HourBucket]] = {}
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

            if event.dst_ip is not None:
                peer = str(event.dst_ip)
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
                "version": 1,
                "config": {
                    "peer_top_k": self.peer_top_k,
                    "port_top_k": self.port_top_k,
                    "interface_top_k": self.interface_top_k,
                    "session_multiplier": self.session_multiplier,
                    "membership_bits": self.membership_bits,
                },
                "watermark": self._watermark.isoformat() if self._watermark else None,
                "devices": devices,
            }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProfileStore:
        if payload.get("version") != 1:
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
        for ip, buckets in self._profiles.items():
            expired = [hour for hour, bucket in buckets.items() if bucket.last_seen <= cutoff]
            for hour in expired:
                removed += buckets[hour].sessions
                del buckets[hour]
            if not buckets:
                empty_devices.append(ip)
        for ip in empty_devices:
            del self._profiles[ip]
        return removed

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
