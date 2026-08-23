from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping


_SYSLOG_HEADER = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+"
)
_KV = re.compile(
    r'(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=(?:"((?:\\.|[^"])*)"?|([^\s]+))'
)
_TZ = re.compile(r"^([+-])(\d{2})(\d{2})$")

_READ_CHUNK_BYTES = 64 * 1024
_MAX_LINE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class FortiEvent:
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


def parse_line(line: str) -> FortiEvent | None:
    """Parse one FortiGate syslog record without doing IO or raising on bad input."""
    try:
        if not isinstance(line, str) or not line.strip():
            return None

        # syslog 的主机头没有 FortiOS 字段语义，先去掉可避免日期里的冒号干扰 KV 扫描。
        payload = _SYSLOG_HEADER.sub("", line.rstrip("\r\n"), count=1)
        fields = _parse_kv(payload)

        event_at = _event_time(fields.get("eventtime"), fields.get("tz"))
        logid = _text(fields.get("logid"))
        event_type = _text(fields.get("type"))
        subtype = _text(fields.get("subtype"))
        level = _text(fields.get("level"))
        if event_at is None or None in (logid, event_type, subtype, level):
            return None

        return FortiEvent(
            at=event_at,
            logid=logid,
            type=event_type,
            subtype=subtype,
            level=level,
            action=_text(fields.get("action")),
            src_ip=_text(fields.get("srcip")),
            dst_ip=_text(fields.get("dstip")),
            src_port=_integer(fields.get("srcport")),
            dst_port=_integer(fields.get("dstport")),
            proto=_integer(fields.get("proto")),
            src_intf=_text(fields.get("srcintf")),
            dst_intf=_text(fields.get("dstintf")),
            user=_text(fields.get("user")),
            logdesc=_text(fields.get("logdesc")),
            msg=_text(fields.get("msg")),
            status=_text(fields.get("status")),
            sent_bytes=_integer(fields.get("sentbyte")),
            rcvd_bytes=_integer(fields.get("rcvdbyte")),
            # 映射保留原始 FortiOS 值，调用方仍能读取尚未提升为固定字段的信息。
            raw=fields,
        )
    except (OverflowError, OSError, TypeError, ValueError):
        # 日志输入不可信，单条字段越界或格式破损不能让持续读取停下来。
        return None


def _parse_kv(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in _KV.finditer(payload):
        key = match.group(1)
        quoted = match.group(2)
        value = quoted if quoted is not None else match.group(3)
        if quoted is not None:
            value = re.sub(r"\\(.)", r"\1", value)
        fields[key] = value
    return fields


def _event_time(raw_ns: str | None, raw_tz: str | None) -> datetime | None:
    if raw_ns is None or raw_tz is None:
        return None
    tz_match = _TZ.fullmatch(raw_tz)
    if tz_match is None:
        return None

    hours = int(tz_match.group(2))
    minutes = int(tz_match.group(3))
    if hours > 23 or minutes > 59:
        return None
    direction = 1 if tz_match.group(1) == "+" else -1
    source_tz = timezone(direction * timedelta(hours=hours, minutes=minutes))

    nanoseconds = int(raw_ns)
    if nanoseconds < 0:
        return None
    seconds, subsecond_ns = divmod(nanoseconds, 1_000_000_000)
    # datetime 只有微秒精度，整数拆分可避免 float 把真实纳秒时间四舍五入到相邻微秒。
    local = datetime.fromtimestamp(seconds, tz=source_tz).replace(
        microsecond=subsecond_ns // 1_000
    )
    return local.astimezone(timezone.utc)


def _text(value: str | None) -> str | None:
    if value is None or value == "N/A":
        return None
    return value


def _integer(value: str | None) -> int | None:
    value = _text(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class FortiLogTailer:
    """Read newly appended FortiGate records while leaving the source untouched."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        checkpoint_path: str | os.PathLike[str] | None = None,
        backfill_bytes: int = 0,
    ) -> None:
        if isinstance(backfill_bytes, bool) or backfill_bytes < 0:
            raise ValueError("backfill_bytes must be a non-negative integer")

        self.path = Path(path)
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        if (
            self.checkpoint_path is not None
            and self.checkpoint_path.resolve() == self.path.resolve()
        ):
            # 配错检查点路径也不能把源日志当成状态文件覆盖。
            raise ValueError("checkpoint_path must differ from the source path")
        self.backfill_bytes = int(backfill_bytes)

        self._inode: int | None = None
        self._offset = 0
        self._committed_offset = 0
        self._buffer = bytearray()
        self._discarding_overlong = False
        self._skip_initial_fragment = False
        self._lines_read = 0
        self._lines_dropped = 0
        self._rotations = 0

        self._set_initial_position()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "lines_read": self._lines_read,
            "lines_dropped": self._lines_dropped,
            "rotations": self._rotations,
            "offset": self._offset,
        }

    def poll(self, *, max_lines: int = 5000) -> list[FortiEvent]:
        if isinstance(max_lines, bool) or max_lines < 0:
            raise ValueError("max_lines must be a non-negative integer")

        events: deque[FortiEvent] = deque()
        try:
            # 二进制 offset 与内核报告的文件大小一致，也不会因坏编码让读取位置漂移。
            with self.path.open("rb") as source:
                stat = os.fstat(source.fileno())
                self._prepare_open_file(stat.st_ino, stat.st_size)
                source.seek(self._offset)

                # 本轮只追到 open 时看到的文件尾，持续写入也不能让一次 poll 无界延长。
                remaining = max(0, stat.st_size - self._offset)
                while remaining:
                    chunk_start = self._offset
                    chunk = source.read(min(_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self._offset += len(chunk)
                    remaining -= len(chunk)
                    self._consume(chunk, chunk_start, events, int(max_lines))
        except (FileNotFoundError, OSError):
            # 轮转的 rename/create 窗口里路径可以短暂不存在，下一次 poll 再接即可。
            return []

        self._write_checkpoint()
        return list(events)

    def _set_initial_position(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            # 文件尚未创建时记住“从头接入”，否则首次出现的小文件会被当成历史跳过。
            return

        checkpoint = self._read_checkpoint()
        self._inode = stat.st_ino
        if checkpoint is not None:
            checkpoint_inode, checkpoint_offset = checkpoint
            if checkpoint_inode == stat.st_ino and 0 <= checkpoint_offset <= stat.st_size:
                self._offset = checkpoint_offset
            else:
                # 检查点指向旧 inode 或已截短内容时，新文件从头读取并显式记轮转。
                self._rotations = 1
                self._offset = 0
        elif self.backfill_bytes:
            self._offset = max(0, stat.st_size - self.backfill_bytes)
        else:
            self._offset = stat.st_size

        self._committed_offset = self._offset
        self._skip_initial_fragment = not self._is_line_boundary(self._offset)

    def _prepare_open_file(self, inode: int, size: int) -> None:
        if self._inode is None:
            self._inode = inode
            self._offset = 0
            self._committed_offset = 0
            return
        if inode != self._inode or size < self._offset:
            self._rotations += 1
            self._inode = inode
            self._offset = 0
            self._committed_offset = 0
            self._buffer.clear()
            self._discarding_overlong = False
            self._skip_initial_fragment = False

    def _consume(
        self,
        chunk: bytes,
        chunk_start: int,
        events: deque[FortiEvent],
        max_lines: int,
    ) -> None:
        cursor = 0
        while True:
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                self._append_fragment(chunk[cursor:])
                return

            fragment = chunk[cursor:newline]
            if not self._discarding_overlong:
                self._buffer.extend(fragment)
            line_end = chunk_start + newline + 1
            self._finish_line(events, max_lines)
            self._committed_offset = line_end
            cursor = newline + 1

    def _append_fragment(self, fragment: bytes) -> None:
        if self._discarding_overlong:
            return
        self._buffer.extend(fragment)
        if len(self._buffer) > _MAX_LINE_BYTES:
            # 无换行的大输入若一直保留会耗尽内存，超过上限后只等边界并记一次丢弃。
            self._buffer.clear()
            self._discarding_overlong = True

    def _finish_line(self, events: deque[FortiEvent], max_lines: int) -> None:
        if self._skip_initial_fragment:
            self._skip_initial_fragment = False
            self._buffer.clear()
            self._discarding_overlong = False
            return

        self._lines_read += 1
        if self._discarding_overlong:
            self._lines_dropped += 1
            self._discarding_overlong = False
            self._buffer.clear()
            return

        raw_line = bytes(self._buffer).rstrip(b"\r")
        self._buffer.clear()
        event = parse_line(raw_line.decode("utf-8", errors="replace"))
        if event is None:
            self._lines_dropped += 1
            return

        if max_lines == 0:
            self._lines_dropped += 1
            return
        if len(events) == max_lines:
            # 交付容量满时淘汰队首，保住离当前时刻最近的证据。
            events.popleft()
            self._lines_dropped += 1
        events.append(event)

    def _is_line_boundary(self, offset: int) -> bool:
        if offset == 0:
            return True
        try:
            with self.path.open("rb") as source:
                source.seek(offset - 1)
                return source.read(1) == b"\n"
        except OSError:
            return False

    def _read_checkpoint(self) -> tuple[int, int] | None:
        if self.checkpoint_path is None:
            return None
        try:
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            inode = data["inode"]
            offset = data["offset"]
            if isinstance(inode, bool) or isinstance(offset, bool):
                return None
            return int(inode), int(offset)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_checkpoint(self) -> None:
        if self.checkpoint_path is None or self._inode is None:
            return
        try:
            self.checkpoint_path.write_text(
                json.dumps(
                    {"inode": self._inode, "offset": self._committed_offset},
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError:
            # 检查点不可写只影响重启续读，不能反过来阻断当前只读日志流。
            return
