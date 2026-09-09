"""GoFlow2 JSON flow records to the shared ClickHouse fact schema.

The collector and endpoint probes are version-pinned deployment components.
This process owns normalization, stable event ids, durable file offsets and
ClickHouse inserts. It never asks a model to construct collector parameters.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from frontend.gateway.ingest.facts_ingest import (
    _build_alert_pipeline,
    commit_live_batch_sinks,
)

FLOW_FILE = Path(os.getenv("AUTOPOIESIS_FLOW_FILE", "/var/lib/autopoiesis-flow/goflow2.jsonl"))
CHECKPOINT_FILE = Path(os.getenv("AUTOPOIESIS_FLOW_CHECKPOINT", "/var/lib/autopoiesis-flow-ingest/checkpoint.json"))
STATUS_FILE = Path(os.getenv("AUTOPOIESIS_FLOW_STATUS", "/run/autopoiesis-flow-ingest/status.json"))
BATCH_SIZE = max(1, int(os.getenv("AUTOPOIESIS_FLOW_BATCH", "1000")))
POLL_SECONDS = max(0.2, float(os.getenv("AUTOPOIESIS_FLOW_POLL_SECONDS", "1")))


def _integer(value: Any, *, low: int = 0, high: int = 2**63 - 1) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return low
    return parsed if low <= parsed <= high else low


def _address(value: Any) -> str:
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return ""


def parse_flow(record: dict[str, Any]) -> list[Any] | None:
    """Normalize one GoFlow2 JSON object into facts_v2 column order."""
    src, dst = _address(record.get("src_addr")), _address(record.get("dst_addr"))
    received_ns = _integer(record.get("time_received_ns"))
    ended_ns = _integer(record.get("time_flow_end_ns"))
    timestamp_ns = ended_ns or received_ns
    if not src or not dst or timestamp_ns <= 0:
        return None
    stamp = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)
    event_ts = stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    protocol = str(record.get("type") or "FLOW").lower()
    sampler = _address(record.get("sampler_address")) or "flow-collector"
    stable = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    event_id = hashlib.sha256(("flow\0" + stable).encode()).hexdigest()
    return [
        event_ts, event_id, sampler, src, dst,
        _integer(record.get("dst_port"), high=65535), str(record.get("proto") or ""),
        "observed", "", "", "traffic", protocol,
        str(record.get("in_if") or record.get("input_if") or ""),
        str(record.get("out_if") or record.get("output_if") or ""), "", "",
        _integer(record.get("bytes")), 0,
    ]


def parse_stream_event(record: dict[str, Any], row: list[Any]) -> dict[str, Any]:
    """Build the deterministic detector envelope for one normalized flow."""
    packets = _integer(record.get("packets"))
    src_mac = str(record.get("src_mac") or "").strip().lower()
    return {
        "schema_version": 1,
        "event_id": row[1],
        "event_ts": str(row[0]).replace(" ", "T") + "Z",
        "parse_status": "ok",
        "source_kind": "real",
        "replay": False,
        "device_key": row[2],
        "src_device_key": src_mac or row[3],
        "type": "traffic",
        "subtype": row[11],
        "action": "observed",
        "proto": record.get("proto") or "",
        "srcip": row[3],
        "srcport": _integer(record.get("src_port"), high=65535),
        "srcintf": row[12],
        "srcmac": src_mac,
        "dstip": row[4],
        "dstport": row[5],
        "dstintf": row[13],
        "dstmac": str(record.get("dst_mac") or "").strip().lower(),
        "vlan": _integer(record.get("vlan_id"), high=4095),
        "bytes_total": row[16],
        "pkts_total": packets,
        "source": {"path": str(FLOW_FILE), "collector": "goflow2"},
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _checkpoint() -> dict[str, Any]:
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(**values: Any) -> None:
    _atomic_json(STATUS_FILE, {"pid": os.getpid(), "checked_at": datetime.now(timezone.utc).isoformat(), **values})


def live() -> None:
    inserted = 0
    rejected = 0
    alert_pipeline = _build_alert_pipeline()
    while True:
        if not FLOW_FILE.exists():
            _write_status(state="waiting_for_collector_file", rows_inserted=inserted, rejected_records=rejected)
            time.sleep(POLL_SECONDS)
            continue
        stat = FLOW_FILE.stat()
        source_id = f"{stat.st_dev}:{stat.st_ino}"
        saved = _checkpoint()
        offset = int(saved.get("offset", 0)) if saved.get("source_id") == source_id else 0
        if offset > stat.st_size:
            offset = 0
        rows: list[list[Any]] = []
        stream_events: list[dict[str, Any]] = []

        def commit_batch() -> None:
            nonlocal inserted
            if not rows and not stream_events:
                return
            commit_live_batch_sinks(
                rows, [], stream_events, alert_pipeline=alert_pipeline
            )
            inserted += len(rows)
            rows.clear()
            stream_events.clear()

        with FLOW_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    handle.seek(line_start)
                    break
                try:
                    value = json.loads(line)
                    row = parse_flow(value) if isinstance(value, dict) else None
                except ValueError:
                    row = None
                if row is None:
                    rejected += 1
                else:
                    rows.append(row)
                    stream_events.append(parse_stream_event(value, row))
                if len(rows) >= BATCH_SIZE:
                    commit_batch()
                offset = handle.tell()
                if offset <= line_start:
                    break
        commit_batch()
        _atomic_json(CHECKPOINT_FILE, {"source_id": source_id, "offset": offset, "path": str(FLOW_FILE)})
        _write_status(
            state="collecting", source_id=source_id, committed_offset=offset,
            source_size=stat.st_size, rows_inserted=inserted,
            rejected_records=rejected, **alert_pipeline.status(),
        )
        time.sleep(POLL_SECONDS)


def status() -> int:
    try:
        value = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {"state": "not_started"}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("state") == "collecting" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=["live"])
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        raise SystemExit(status())
    if args.mode != "live":
        parser.error("live mode or --status is required")
    live()
