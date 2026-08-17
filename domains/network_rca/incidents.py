"""Historical network incidents, deterministic detectors, and isolated replay.

The committed fixture records manually supplied historical facts. It deliberately
contains no invented occurrence timestamps, hardware MAC addresses, or successful
post-remediation readback. Synthetic detector inputs and replay events are tagged
``simulated`` and can never be represented as current online observations.
"""
from __future__ import annotations

import copy
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "real_incidents.json"
SOURCE_KINDS = {"observed", "manual", "readback", "simulated"}
DISPOSITION_STATUSES = {
    "documented",
    "triaged",
    "mitigating",
    "awaiting_readback",
    "resolved",
}


class IncidentReadbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["passed", "failed", "partial"]
    source_kind: Literal["readback"] = "readback"
    collected_at: datetime
    checks: list[dict[str, Any]] = Field(min_length=1)

    @field_validator("collected_at")
    @classmethod
    def collected_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must include a timezone")
        return value

    @model_validator(mode="after")
    def passing_readback_requires_all_checks_to_pass(self) -> "IncidentReadbackUpdate":
        malformed = [
            index
            for index, check in enumerate(self.checks)
            if not isinstance(check.get("check"), str)
            or not check["check"].strip()
            or not isinstance(check.get("passed"), bool)
        ]
        if malformed:
            raise ValueError(f"readback checks require non-empty check and boolean passed: {malformed}")
        if self.state == "passed" and any(check["passed"] is not True for check in self.checks):
            raise ValueError("passed readback requires every check to pass")
        return self


class IncidentDispositionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "documented", "triaged", "mitigating", "awaiting_readback", "resolved"
    ]
    operator_note: str | None = Field(default=None, max_length=2000)
    updated_at: datetime | None = None
    readback: IncidentReadbackUpdate | None = None

    @field_validator("updated_at")
    @classmethod
    def updated_at_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("updated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def resolved_requires_passing_readback(self) -> "IncidentDispositionUpdate":
        if self.status == "resolved" and (
            self.readback is None or self.readback.state != "passed"
        ):
            raise ValueError("resolved status requires a passed readback")
        if self.status == "resolved" and self.updated_at is None:
            raise ValueError("resolved status requires updated_at")
        if (
            self.status == "resolved"
            and self.readback is not None
            and self.updated_at is not None
            and self.updated_at < self.readback.collected_at
        ):
            raise ValueError("resolved updated_at cannot be earlier than readback collected_at")
        return self


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _observation_scope(source_kinds: set[str]) -> str:
    if source_kinds == {"simulated"}:
        return "replay_simulation"
    if "observed" in source_kinds or "readback" in source_kinds:
        return "collected_evidence"
    return "historical_manual_record"


def detect_dual_mac_window(
    events: list[dict[str, Any]], window_seconds: int = 300
) -> dict[str, Any]:
    """Find two distinct MACs claiming one IP inside an inclusive time window."""
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    normalized: list[tuple[datetime, str, str, str, str]] = []
    for index, event in enumerate(events):
        ip = str(event.get("ip") or "").strip()
        mac = str(event.get("mac") or "").strip().lower()
        timestamp = event.get("timestamp") or event.get("event_ts")
        source_kind = str(event.get("source_kind") or "observed")
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported evidence source_kind: {source_kind}")
        if not ip or not mac or not timestamp:
            continue
        normalized.append(
            (
                _parse_timestamp(str(timestamp)),
                ip,
                mac,
                str(event.get("event_id") or f"event-{index + 1}"),
                source_kind,
            )
        )
    normalized.sort(key=lambda row: row[0])

    active: dict[str, deque[tuple[datetime, str, str, str]]] = defaultdict(deque)
    findings_by_ip: dict[str, dict[str, Any]] = {}
    for timestamp, ip, mac, event_id, source_kind in normalized:
        window = active[ip]
        cutoff = timestamp - timedelta(seconds=window_seconds)
        while window and window[0][0] < cutoff:
            window.popleft()
        window.append((timestamp, mac, event_id, source_kind))
        macs = sorted({row[1] for row in window})
        if len(macs) < 2:
            continue
        candidate = {
            "ip": ip,
            "macs": macs,
            "first_seen": window[0][0].isoformat().replace("+00:00", "Z"),
            "last_seen": window[-1][0].isoformat().replace("+00:00", "Z"),
            "evidence_ids": [row[2] for row in window],
            "source_kinds": sorted({row[3] for row in window}),
        }
        previous = findings_by_ip.get(ip)
        if previous is None or len(candidate["evidence_ids"]) > len(previous["evidence_ids"]):
            findings_by_ip[ip] = candidate

    source_kinds = {row[4] for row in normalized}
    return {
        "detector": "dual_mac_window",
        "detected": bool(findings_by_ip),
        "window_seconds": window_seconds,
        "evaluated_events": len(normalized),
        "findings": [findings_by_ip[ip] for ip in sorted(findings_by_ip)],
        "source_kinds": sorted(source_kinds),
        "observation_scope": _observation_scope(source_kinds),
        "current_online_observation": False,
    }


def detect_host_network_preflight(
    *,
    expected_business_ips: list[str],
    cloud_init_enabled: bool,
    cloud_init_network_disabled: bool,
    persistent_business_ips: list[str],
    source_kind: str = "observed",
) -> dict[str, Any]:
    """Detect reboot risk while the intended IP is absent from persistent config."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unsupported evidence source_kind: {source_kind}")
    expected = sorted(set(expected_business_ips))
    persistent = sorted(set(persistent_business_ips))
    missing = sorted(set(expected) - set(persistent))
    risks: list[str] = []
    if cloud_init_enabled and not cloud_init_network_disabled:
        risks.append("cloud_init_can_manage_network_on_restart")
    if missing:
        risks.append("expected_business_ip_absent_from_persistent_config")
    return {
        "detector": "host_network_preflight",
        "detected": bool(risks),
        "risks": risks,
        "expected_business_ips": expected,
        "persistent_business_ips": persistent,
        "missing_from_persistent_config": missing,
        "source_kinds": [source_kind],
        "observation_scope": _observation_scope({source_kind}),
        "current_online_observation": False,
    }


def detect_host_network_drift(
    *,
    expected_business_ips: list[str],
    actual_business_ips: list[str],
    source_kind: str = "readback",
) -> dict[str, Any]:
    """Compare expected business IPs with a post-change address readback."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unsupported evidence source_kind: {source_kind}")
    expected = sorted(set(expected_business_ips))
    actual = sorted(set(actual_business_ips))
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    return {
        "detector": "host_network_drift",
        "detected": bool(missing),
        "expected_business_ips": expected,
        "actual_business_ips": actual,
        "missing_business_ips": missing,
        "unexpected_business_ips": unexpected,
        "source_kinds": [source_kind],
        "observation_scope": _observation_scope({source_kind}),
        "current_online_observation": False,
    }


def evaluate_incident_detector(incident: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the committed detector input while preserving its provenance."""
    detector = incident["detector"]
    kind = detector["kind"]
    if kind == "dual_mac_window":
        return detect_dual_mac_window(
            detector["events"], int(detector.get("window_seconds", 300))
        )
    if kind == "host_network_preflight_drift":
        source_kind = detector["input_source_kind"]
        expected = detector["expected_business_ips"]
        preflight = detect_host_network_preflight(
            expected_business_ips=expected,
            source_kind=source_kind,
            **detector["preflight"],
        )
        drift = detect_host_network_drift(
            expected_business_ips=expected,
            actual_business_ips=detector["after_restart"]["actual_business_ips"],
            source_kind=source_kind,
        )
        return {
            "detector": kind,
            "detected": preflight["detected"] or drift["detected"],
            "preflight": preflight,
            "drift": drift,
            "source_kinds": [source_kind],
            "observation_scope": _observation_scope({source_kind}),
            "current_online_observation": False,
        }
    raise ValueError(f"unsupported incident detector: {kind}")


def _validate_fixture(payload: dict[str, Any]) -> None:
    if payload.get("current_online_observation") is not False:
        raise ValueError("historical fixture cannot claim current online observation")
    incidents = payload.get("incidents")
    if not isinstance(incidents, list) or not incidents:
        raise ValueError("incident fixture must contain incidents")
    ids: set[str] = set()
    all_evidence_ids: set[str] = set()
    for incident in incidents:
        incident_id = incident.get("id")
        if not incident_id or incident_id in ids:
            raise ValueError("incident ids must be unique and non-empty")
        ids.add(incident_id)
        if incident.get("classification") != "historical_real_incident":
            raise ValueError(f"{incident_id}: invalid classification")
        if incident.get("current_online_observation") is not False:
            raise ValueError(f"{incident_id}: cannot claim current observation")
        timeline = incident.get("evidence_timeline") or []
        if [item.get("sequence") for item in timeline] != list(range(1, len(timeline) + 1)):
            raise ValueError(f"{incident_id}: evidence sequence must be contiguous")
        for item in timeline:
            if item.get("source_kind") not in SOURCE_KINDS:
                raise ValueError(f"{incident_id}: invalid evidence source kind")
            if not item.get("source_type") or not item.get("evidence_locator"):
                raise ValueError(f"{incident_id}: evidence requires source type and locator")
            evidence_id = item.get("evidence_id")
            if not evidence_id or evidence_id in all_evidence_ids:
                raise ValueError(f"{incident_id}: evidence ids must be unique and non-empty")
            all_evidence_ids.add(evidence_id)
        local_evidence_ids = {item["evidence_id"] for item in timeline}
        unknown_root_refs = set(incident.get("root_cause_evidence_ids") or []) - local_evidence_ids
        if unknown_root_refs:
            raise ValueError(f"{incident_id}: unknown root-cause evidence ids: {unknown_root_refs}")
        disposition = incident.get("disposition") or {}
        if disposition.get("status") not in DISPOSITION_STATUSES:
            raise ValueError(f"{incident_id}: invalid disposition status")
        readback = disposition.get("readback") or {}
        if disposition.get("status") == "resolved":
            if readback.get("state") != "passed" or readback.get("source_kind") != "readback":
                raise ValueError(f"{incident_id}: resolved incident requires passed readback")
            checks = readback.get("checks") or []
            if not checks or any(check.get("passed") is not True for check in checks):
                raise ValueError(f"{incident_id}: resolved readback checks must all pass")
            collected_at = _parse_timestamp(str(readback.get("collected_at") or ""))
            updated_at = _parse_timestamp(str(disposition.get("updated_at") or ""))
            if updated_at < collected_at:
                raise ValueError(f"{incident_id}: disposition predates readback")


def load_incident_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_fixture(payload)
    return payload


class IncidentRepository:
    """Thread-safe process-local disposition state over an immutable fixture."""

    def __init__(
        self,
        fixture_path: Path = FIXTURE_PATH,
        *,
        ledger_path: Path | None = None,
    ) -> None:
        self._payload = load_incident_fixture(fixture_path)
        self._incidents = {item["id"]: item for item in self._payload["incidents"]}
        self._lock = RLock()
        self._ledger_path = ledger_path
        self._ledger_errors: list[str] = []
        self._replay_disposition_ledger()

    @property
    def ledger_errors(self) -> list[str]:
        with self._lock:
            return list(self._ledger_errors)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in self._payload.items()
            if key != "incidents"
        }

    def list(
        self,
        *,
        status: str | None = None,
        incident_type: str | None = None,
        asset_ip: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._incidents.values())
            if status:
                items = [item for item in items if item["disposition"]["status"] == status]
            if incident_type:
                items = [item for item in items if item["incident_type"] == incident_type]
            if asset_ip:
                items = [item for item in items if item["asset"]["ip"] == asset_ip]
            return [self._summary(item) for item in items]

    def get(self, incident_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                incident = copy.deepcopy(self._incidents[incident_id])
            except KeyError as exc:
                raise KeyError(incident_id) from exc
        incident["detection"] = evaluate_incident_detector(incident)
        incident.update(_incident_ui_fields(incident))
        return incident

    def update_disposition(
        self, incident_id: str, update: IncidentDispositionUpdate
    ) -> dict[str, Any]:
        with self._lock:
            if incident_id not in self._incidents:
                raise KeyError(incident_id)
            update_payload = update.model_dump(mode="json")
            if update_payload["updated_at"] is None:
                update_payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
            normalized = IncidentDispositionUpdate.model_validate(update_payload).model_dump(
                mode="json"
            )
            self._append_disposition_update(incident_id, normalized)
            self._apply_disposition_update(incident_id, normalized)
        return self.get(incident_id)

    def _apply_disposition_update(
        self, incident_id: str, update_payload: dict[str, Any]
    ) -> None:
        disposition = self._incidents[incident_id]["disposition"]
        disposition["status"] = update_payload["status"]
        disposition["operator_note"] = update_payload["operator_note"]
        disposition["updated_at"] = update_payload["updated_at"]
        if update_payload["readback"] is not None:
            disposition["readback"] = update_payload["readback"]

    def _append_disposition_update(
        self, incident_id: str, update_payload: dict[str, Any]
    ) -> None:
        if self._ledger_path is None:
            return
        entry = {
            "schema_version": "1.0",
            "incident_id": incident_id,
            "updated_at": update_payload["updated_at"],
            "update": update_payload,
        }
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        needs_separator = False
        if self._ledger_path.exists() and self._ledger_path.stat().st_size:
            with self._ledger_path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                needs_separator = existing.read(1) != b"\n"
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            if needs_separator:
                handle.write("\n")
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _replay_disposition_ledger(self) -> None:
        if self._ledger_path is None or not self._ledger_path.exists():
            return
        with self._lock:
            with self._ledger_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        incident_id = entry["incident_id"]
                        if incident_id not in self._incidents:
                            raise ValueError("unknown incident_id")
                        update = IncidentDispositionUpdate.model_validate(entry["update"])
                        payload = update.model_dump(mode="json")
                        if entry.get("updated_at") != payload["updated_at"]:
                            raise ValueError("ledger updated_at does not match update")
                        self._apply_disposition_update(incident_id, payload)
                    except (KeyError, TypeError, ValueError) as exc:
                        self._ledger_errors.append(
                            f"line {line_number}: {type(exc).__name__}: {exc}"
                        )

    @staticmethod
    def _summary(incident: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "id": incident["id"],
                "title": incident["title"],
                "incident_type": incident["incident_type"],
                "severity": incident["severity"],
                "asset": incident["asset"],
                "classification": incident["classification"],
                "summary": incident["summary"],
                "disposition": incident["disposition"],
                "current_online_observation": False,
                "evidence_count": len(incident["evidence_timeline"]),
            }
        )


_REPLAY_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _incident_ui_fields(incident: dict[str, Any]) -> dict[str, Any]:
    """Build the audit-oriented detail contract consumed directly by the console."""
    evidence = [
        {
            "id": item["evidence_id"],
            "sourceKind": item["source_kind"],
            "sourceType": item.get("source_type"),
            "locator": item.get("evidence_locator"),
            "capturedAt": item.get("occurred_at"),
            "summary": item["summary"],
        }
        for item in incident["evidence_timeline"]
    ]
    timeline = [
        {
            "id": f"timeline-{item['sequence']}",
            "sequence": item["sequence"],
            "capturedAt": item.get("occurred_at"),
            "phase": item["phase"],
            "summary": item["summary"],
            "evidenceIds": [item["evidence_id"]],
            "sourceKind": item["source_kind"],
        }
        for item in incident["evidence_timeline"]
    ]
    if incident["incident_type"] == "dual_mac_arp_drift":
        metrics, topology = _dual_mac_ui(incident)
    else:
        metrics, topology = _host_network_ui(incident)
    readbacks = []
    for check in incident["disposition"]["readback"]["checks"]:
        readbacks.append(
            {
                "id": check["check"],
                "passed": check["passed"],
                "observed": check.get("observed"),
                "evidenceIds": [check["evidence_id"]] if check.get("evidence_id") else [],
                "missingReason": None if check.get("evidence_id") else "readback_not_collected",
            }
        )
    return {
        "capturedAt": incident.get("captured_at"),
        "rootCause": incident["root_cause"],
        "impact": incident["impact"],
        "rootCauseEvidenceIds": incident["root_cause_evidence_ids"],
        "metrics": metrics,
        "timeline": timeline,
        "evidence": evidence,
        "topology": topology,
        "response": {
            "approvalRequired": True,
            "steps": _response_steps(incident),
            "readbacks": readbacks,
        },
    }


def _dual_mac_ui(incident: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    arp_id = "observed-192-168-1-23-arp-sample-window"
    ssh_id = "observed-192-168-1-23-owner-ssh-key-correlation"
    arp_facts = next(
        item["facts"] for item in incident["evidence_timeline"] if item["evidence_id"] == arp_id
    )
    ssh_facts = next(
        item["facts"] for item in incident["evidence_timeline"] if item["evidence_id"] == ssh_id
    )
    macs = arp_facts["macs"]
    metrics = [
        {
            "id": "unique_mac_count",
            "label": "Unique MAC owners",
            "unit": "count",
            "points": [
                {
                    "capturedAt": arp_facts["window_start"],
                    "value": len(macs),
                    "evidenceIds": [arp_id],
                }
            ],
        },
        {
            "id": "arp_owner",
            "label": "ARP owner",
            "unit": "mac",
            "points": [
                {"capturedAt": timestamp, "value": mac, "evidenceIds": [arp_id]}
                for timestamp, mac in arp_facts["samples"]
            ],
        },
        {
            "id": "ssh_host_key_observed",
            "label": "SSH ED25519 host key observed",
            "unit": "boolean",
            "points": [
                {"capturedAt": timestamp, "value": present, "evidenceIds": [ssh_id]}
                for timestamp, _mac, present in ssh_facts["samples"]
            ],
        },
    ]
    nodes = [
        {
            "id": "ip-192-168-1-23",
            "kind": "ip",
            "label": "192.168.1.23",
            "evidenceIds": [arp_id],
            "sourceKind": "observed",
        }
    ] + [
        {
            "id": f"mac-{mac.lower().replace(':', '-')}",
            "kind": "mac",
            "label": mac,
            "evidenceIds": [arp_id],
            "sourceKind": "observed",
        }
        for mac in macs
    ]
    edges = [
        {
            "source": f"mac-{mac.lower().replace(':', '-')}",
            "target": "ip-192-168-1-23",
            "kind": "claims_ip",
            "evidenceIds": [arp_id],
            "sourceKind": "observed",
        }
        for mac in macs
    ]
    return metrics, {"nodes": nodes, "edges": edges}


def _host_network_ui(incident: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rewrite_id = "observed-192-168-1-27-cloud-init-rewrite"
    missing_id = "manual-192-168-1-27-business-ip-missing"
    present_id = "readback-192-168-1-27-business-ip-present"
    rewrite = next(
        item for item in incident["evidence_timeline"] if item["evidence_id"] == rewrite_id
    )
    present = next(
        item for item in incident["evidence_timeline"] if item["evidence_id"] == present_id
    )
    metrics = [
        {
            "id": "netplan_file_bytes",
            "label": "cloud-init netplan bytes",
            "unit": "bytes",
            "points": [
                {
                    "capturedAt": rewrite["occurred_at"],
                    "value": rewrite["facts"]["bytes_before"],
                    "evidenceIds": [rewrite_id],
                },
                {
                    "capturedAt": rewrite["occurred_at"],
                    "value": rewrite["facts"]["bytes_after"],
                    "evidenceIds": [rewrite_id],
                },
            ],
        },
        {
            "id": "business_ip_present",
            "label": "Business IP present",
            "unit": "boolean",
            "points": [
                {"capturedAt": None, "value": False, "evidenceIds": [missing_id]},
                {
                    "capturedAt": present["occurred_at"],
                    "value": True,
                    "evidenceIds": [present_id],
                },
            ],
        },
    ]
    nodes = [
        ("host-192-168-1-27", "host", "192.168.1.27", present_id, "readback"),
        ("cloud-init", "network_manager", "cloud-init", rewrite_id, "observed"),
        ("business-ip", "ip", "192.168.1.27/24", present_id, "readback"),
    ]
    return metrics, {
        "nodes": [
            {
                "id": node_id,
                "kind": kind,
                "label": label,
                "evidenceIds": [evidence_id],
                "sourceKind": source_kind,
            }
            for node_id, kind, label, evidence_id, source_kind in nodes
        ],
        "edges": [
            {
                "source": "cloud-init",
                "target": "host-192-168-1-27",
                "kind": "rewrote_network_config",
                "evidenceIds": [rewrite_id],
                "sourceKind": "observed",
            },
            {
                "source": "host-192-168-1-27",
                "target": "business-ip",
                "kind": "restored_address",
                "evidenceIds": [present_id],
                "sourceKind": "readback",
            },
        ],
    }


def _response_steps(incident: dict[str, Any]) -> list[dict[str, Any]]:
    if incident["incident_type"] == "dual_mac_arp_drift":
        return [
            {"id": "identify_ports", "status": "pending", "evidenceIds": []},
            {"id": "remove_duplicate_owner", "status": "approval_required", "evidenceIds": []},
            {"id": "observe_stable_window", "status": "pending", "evidenceIds": []},
        ]
    return [
        {
            "id": "disable_cloud_init_network",
            "status": "completed",
            "evidenceIds": ["observed-192-168-1-27-cloud-init-disabled"],
        },
        {
            "id": "persist_static_network",
            "status": "completed",
            "evidenceIds": ["observed-192-168-1-27-static-netplan"],
        },
        {
            "id": "controlled_restart_persistence",
            "status": "approval_required",
            "evidenceIds": [],
        },
    ]


def incident_replay_events(incident: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a historical incident's signals into the existing isolated fact schema."""
    events: list[dict[str, Any]] = []
    incident_id = incident["id"]
    for index, signal in enumerate(incident["replay_signals"], start=1):
        timestamp = _REPLAY_BASE + timedelta(seconds=int(signal["at_seconds"]))
        ip = str(signal.get("ip") or incident["asset"]["ip"])
        event = {
            "event_id": f"{incident_id}-fixture-{index:03d}",
            "event_ts": timestamp.isoformat().replace("+00:00", "Z"),
            "device_key": f"incident:{incident_id}",
            "src_device_key": f"incident:{incident_id}",
            "srcip": ip,
            "dstip": ip,
            "dstport": 0,
            "proto": 0,
            "action": signal["signal"],
            "service": "ARP" if "arp" in signal["signal"] else "host-network",
            "app": "",
            "type": "event",
            "subtype": "arp" if "arp" in signal["signal"] else "system",
            "srcintf": "",
            "dstintf": "",
            "srcname": str(signal.get("mac") or ""),
            "sentbyte": 0,
            "rcvdbyte": 0,
            "ip": ip,
            "mac": signal.get("mac"),
            "relative_seconds": int(signal["at_seconds"]),
            "time_basis": "simulated_relative_clock",
            "source_kind": "simulated",
            "replay": True,
            "case_id": incident_id,
            "incident_id": incident_id,
            "current_online_observation": False,
            "fault_context": {
                "is_fault": True,
                "scenario": incident_id,
                "confidence": 1.0,
                "source_kind": "simulated",
            },
        }
        events.append(event)
    return events


def build_incident_replay(repository: IncidentRepository, incident_id: str) -> dict[str, Any]:
    incident = repository.get(incident_id)
    events = incident_replay_events(incident)
    detection = incident["detection"]
    if incident["incident_type"] == "dual_mac_arp_drift":
        detection = detect_dual_mac_window(
            [
                {
                    "event_id": event["event_id"],
                    "timestamp": event["event_ts"],
                    "ip": event["ip"],
                    "mac": event["mac"],
                    "source_kind": "simulated",
                }
                for event in events
            ],
            int(incident["detector"].get("window_seconds", 300)),
        )
    return {
        "incident_id": incident_id,
        "replay": True,
        "source_kind": "simulated",
        "time_basis": "simulated_relative_clock",
        "current_online_observation": False,
        "events": events,
        "detection": detection,
    }
