from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from domains.network_rca.fortigate_stream import FortiLogTailer, parse_line


_TRAFFIC_SAMPLE = (
    'Aug 23 15:55:51 _gateway date=2026-08-23 time=15:55:51 '
    'devname="DAHUA_FORTIGATE" devid="FG100ETK20014183" logid="0001000014" '
    'type="traffic" subtype="local" level="notice" vd="root" '
    'eventtime=1787493351468806055 tz="+0200" srcip=192.168.16.96 '
    'srcport=28689 srcintf="port5" srcintfrole="lan" dstip=255.255.255.255 '
    'dstport=28689 dstintf="unknown0" dstintfrole="undefined" '
    'sessionid=127657661 proto=17 action="deny" policyid=0'
)

_ADMIN_SAMPLE = (
    'Aug 23 15:56:05 _gateway date=2026-08-23 time=15:56:05 '
    'devname="DAHUA_FORTIGATE" devid="FG100ETK20014183" logid="0100032002" '
    'type="event" subtype="system" level="alert" vd="root" '
    'eventtime=1787493365290855265 tz="+0200" logdesc="Admin login failed" '
    'sn="0" user="mike" ui="https(45.74.28.226)" method="https" '
    'srcip=45.74.28.226 dstip=77.236.99.125 action="login" status="failed" '
    'reason="name_invalid" msg="Administrator m...'
)

# 契约中的第三条省略了公共信封；补齐解析必需字段后，业务字段仍逐字使用原样本。
_VPN_SAMPLE = (
    'Aug 23 15:55:52 _gateway date=2026-08-23 time=15:55:52 '
    'devname="DAHUA_FORTIGATE" devid="FG100ETK20014183" logid="0101039948" '
    'eventtime=1787493352468806055 tz="+0200" type="event" subtype="vpn" '
    'level="information" logdesc="SSL VPN new connection" action="ssl-new-con" '
    'tunneltype="ssl" remip=185.136.15.82 user="N/A" msg="SSL new connection"'
)


def test_parse_real_traffic_sample_field_by_field() -> None:
    event = parse_line(_TRAFFIC_SAMPLE)

    assert event is not None
    assert event.at == datetime(2026, 8, 23, 13, 55, 51, 468806, tzinfo=timezone.utc)
    assert event.logid == "0001000014"
    assert event.type == "traffic"
    assert event.subtype == "local"
    assert event.level == "notice"
    assert event.action == "deny"
    assert event.src_ip == "192.168.16.96"
    assert event.dst_ip == "255.255.255.255"
    assert event.src_port == 28689
    assert event.dst_port == 28689
    assert event.proto == 17
    assert event.src_intf == "port5"
    assert event.dst_intf == "unknown0"
    assert event.user is None
    assert event.logdesc is None
    assert event.msg is None
    assert event.status is None
    assert event.sent_bytes is None
    assert event.rcvd_bytes is None
    assert event.raw["devname"] == "DAHUA_FORTIGATE"
    assert event.raw["sessionid"] == "127657661"
    assert event.raw["policyid"] == "0"


def test_parse_real_admin_sample_field_by_field() -> None:
    event = parse_line(_ADMIN_SAMPLE)

    assert event is not None
    assert event.at == datetime(2026, 8, 23, 13, 56, 5, 290855, tzinfo=timezone.utc)
    assert event.logid == "0100032002"
    assert event.type == "event"
    assert event.subtype == "system"
    assert event.level == "alert"
    assert event.action == "login"
    assert event.src_ip == "45.74.28.226"
    assert event.dst_ip == "77.236.99.125"
    assert event.src_port is None
    assert event.dst_port is None
    assert event.proto is None
    assert event.src_intf is None
    assert event.dst_intf is None
    assert event.user == "mike"
    assert event.logdesc == "Admin login failed"
    assert event.msg == "Administrator m..."
    assert event.status == "failed"
    assert event.sent_bytes is None
    assert event.rcvd_bytes is None
    assert event.raw["reason"] == "name_invalid"
    assert event.raw["ui"] == "https(45.74.28.226)"


def test_parse_real_vpn_sample_normalizes_na_field_by_field() -> None:
    event = parse_line(_VPN_SAMPLE)

    assert event is not None
    assert event.at == datetime(2026, 8, 23, 13, 55, 52, 468806, tzinfo=timezone.utc)
    assert event.logid == "0101039948"
    assert event.type == "event"
    assert event.subtype == "vpn"
    assert event.level == "information"
    assert event.action == "ssl-new-con"
    assert event.src_ip is None
    assert event.dst_ip is None
    assert event.src_port is None
    assert event.dst_port is None
    assert event.proto is None
    assert event.src_intf is None
    assert event.dst_intf is None
    assert event.user is None
    assert event.logdesc == "SSL VPN new connection"
    assert event.msg == "SSL new connection"
    assert event.status is None
    assert event.sent_bytes is None
    assert event.rcvd_bytes is None
    assert event.raw["user"] == "N/A"
    assert event.raw["remip"] == "185.136.15.82"
    assert event.raw["tunneltype"] == "ssl"


def test_parse_bad_and_empty_lines_returns_none() -> None:
    assert parse_line("") is None
    assert parse_line("not a FortiGate record") is None
    assert parse_line('logid="x" type="event" subtype="system" level="alert"') is None
    assert parse_line(_TRAFFIC_SAMPLE.replace('tz="+0200"', 'tz="bad"')) is None
    assert parse_line(_TRAFFIC_SAMPLE.replace("1787493351468806055", "not-a-number")) is None


def _record(sequence: int) -> str:
    return (
        "Aug 23 15:55:51 _gateway date=2026-08-23 time=15:55:51 "
        f'logid="{sequence:010d}" type="traffic" subtype="local" level="notice" '
        f'eventtime={1787493351468806055 + sequence} tz="+0200" '
        f"srcip=192.0.2.{sequence} srcport={sequence} action=deny"
    )


def _append(path: Path, data: str | bytes) -> None:
    encoded = data.encode("utf-8") if isinstance(data, str) else data
    with path.open("ab") as handle:
        handle.write(encoded)


def test_default_start_at_end_does_not_read_history(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    source.write_text(_record(1) + "\n", encoding="utf-8")
    historical_size = source.stat().st_size

    tailer = FortiLogTailer(source)

    assert tailer.stats["offset"] == historical_size
    assert tailer.poll() == []
    assert tailer.stats["lines_read"] == 0
    _append(source, _record(2) + "\n")
    events = tailer.poll()
    assert [event.logid for event in events] == ["0000000002"]


def test_partial_line_waits_and_checkpoint_stays_at_complete_boundary(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    checkpoint = tmp_path / "checkpoint.json"
    source.touch()
    tailer = FortiLogTailer(source, checkpoint_path=checkpoint)
    record = _record(3)
    split = len(record) // 2

    _append(source, record[:split])
    assert tailer.poll() == []
    assert tailer.stats["lines_read"] == 0
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["offset"] == 0

    # 新实例模拟进程重启，安全检查点会重新读取已落盘的前半行。
    restarted = FortiLogTailer(source, checkpoint_path=checkpoint)
    _append(source, record[split:] + "\n")
    events = restarted.poll()
    assert [event.logid for event in events] == ["0000000003"]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved == {"inode": os.stat(source).st_ino, "offset": source.stat().st_size}


def test_bad_empty_and_overlong_lines_do_not_break_poll(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    source.touch()
    tailer = FortiLogTailer(source)

    _append(source, b"\nnot a record\n" + b"x" * (300 * 1024) + b"\n")
    assert tailer.poll() == []
    assert tailer.stats["lines_read"] == 3
    assert tailer.stats["lines_dropped"] == 3


def test_rotation_switches_to_new_inode_and_reads_from_head(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    rotated = tmp_path / "fortigate.log.1"
    source.touch()
    tailer = FortiLogTailer(source)

    _append(source, _record(4) + "\n")
    assert [event.logid for event in tailer.poll()] == ["0000000004"]
    source.rename(rotated)
    source.write_text(_record(5) + "\n", encoding="utf-8")

    events = tailer.poll()
    assert [event.logid for event in events] == ["0000000005"]
    assert tailer.stats["rotations"] == 1
    assert tailer.stats["offset"] == source.stat().st_size


def test_backpressure_discards_oldest_and_counts_exactly(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    source.touch()
    tailer = FortiLogTailer(source)
    _append(source, "".join(_record(sequence) + "\n" for sequence in range(1, 6)))

    events = tailer.poll(max_lines=2)

    assert [event.logid for event in events] == ["0000000004", "0000000005"]
    assert tailer.stats["lines_read"] == 5
    assert tailer.stats["lines_dropped"] == 3


def test_explicit_backfill_is_bounded_and_skips_leading_fragment(tmp_path: Path) -> None:
    source = tmp_path / "fortigate.log"
    records = [_record(sequence) + "\n" for sequence in range(1, 4)]
    source.write_text("".join(records), encoding="utf-8")

    # 起点落在第二条中间时只能交付边界之后的完整第三条。
    backfill = len(records[2].encode("utf-8")) + 20
    tailer = FortiLogTailer(source, backfill_bytes=backfill)
    events = tailer.poll()

    assert [event.logid for event in events] == ["0000000003"]
    assert tailer.stats["lines_read"] == 1
