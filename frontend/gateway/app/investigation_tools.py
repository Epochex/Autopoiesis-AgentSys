"""Read-only network evidence adapters used by a live investigation."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Any, Callable

from domains.network_rca.fortigate_api import FortiGateReadonlyAPI


def _is_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def collect_fortigate_context(
    subject: str | None,
    *,
    api_factory: Callable[..., FortiGateReadonlyAPI] = FortiGateReadonlyAPI.from_env,
    device_status_reader: Callable[[str, str], dict[str, Any] | None] | None = None,
    live_flow_reader: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Return a bounded router-side snapshot without exposing credentials.

    The FortiGate has much more state than a model prompt should receive.  This
    adapter keeps only the fields that can change an investigation: the target's
    current identity and sessions, interface/VLAN state, policy direction/action,
    and the latest configuration-revision metadata.
    """
    if device_status_reader is None or live_flow_reader is None:
        from .live_identity import device_status, live_flows

        device_status_reader = device_status_reader or device_status
        live_flow_reader = live_flow_reader or live_flows

    api = api_factory(verify_tls=False, timeout=5.0, retries=1)
    snapshot = api.fetch_all()
    devices = snapshot["devices"].get("items") or []
    target_devices = [
        item
        for item in devices
        if subject and subject in {str(item.get("ip") or ""), str(item.get("mac") or "")}
    ][:8]
    result: dict[str, Any] = {
        "fetched_at": snapshot["fetched_at"],
        "degraded": bool(snapshot.get("degraded")),
        "availability": {
            key: {
                "available": bool(snapshot[key].get("available")),
                "degraded": bool(snapshot[key].get("degraded")),
                "missing": list(snapshot[key].get("missing") or ()),
            }
            for key in ("interfaces", "devices", "policies", "changes")
        },
        "target_devices": target_devices,
        "interfaces": list(snapshot["interfaces"].get("items") or ())[:64],
        "policies": list(snapshot["policies"].get("items") or ())[:128],
        "changes": list(snapshot["changes"].get("items") or ())[:20],
        "inventory_count": len(devices),
    }
    if _is_ip(subject):
        result["target_status"] = device_status_reader(subject, "zh")
        result["target_flows"] = live_flow_reader(subject)
    return result


def collect_case_flow_window(
    facts: dict[str, Any],
    incident_start: str | None,
    incident_end: str | None,
) -> dict[str, Any]:
    """Return the exact ClickHouse slice around one triggering flow tuple."""
    from .history import _CH_DB, _q

    source = str(facts.get("sourceIp") or "")
    destination = str(facts.get("destinationIp") or "")
    if not _is_ip(source) or not _is_ip(destination):
        return {"available": False, "reason": "source_and_destination_ip_required"}
    try:
        start = datetime.fromisoformat(str(incident_start or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(incident_end or "").replace("Z", "+00:00"))
    except ValueError:
        return {"available": False, "reason": "incident_window_required"}
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start_text = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    end_text = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    conditions = [
        f"srcip='{source}'",
        f"dstip='{destination}'",
        f"event_ts >= toDateTime64('{start_text}',3)",
        f"event_ts <= toDateTime64('{end_text}',3)",
    ]
    destination_port = facts.get("destinationPort")
    try:
        port = int(destination_port) if destination_port not in (None, "") else 0
    except (TypeError, ValueError):
        port = 0
    if 0 < port <= 65535:
        conditions.append(f"dstport={port}")
    protocol = facts.get("protocol")
    try:
        protocol_number = int(protocol) if protocol not in (None, "") else 0
    except (TypeError, ValueError):
        protocol_number = 0
    if protocol_number > 0:
        conditions.append(f"proto='{protocol_number}'")
    where = " AND ".join(conditions)
    rows = _q(
        "SELECT count() AS flows, countIf(action='deny') AS denied, "
        "countIf(action='accept') AS accepted, groupUniqArray(8)(action) AS actions, "
        "groupUniqArray(8)(srcintf) AS source_interfaces, "
        "groupUniqArray(8)(dstintf) AS destination_interfaces, "
        "min(event_ts) AS first_seen, max(event_ts) AS last_seen "
        f"FROM {_CH_DB}.facts WHERE {where}"
    )
    row = rows[0] if rows else {}
    return {
        "available": True,
        "queryScope": {
            "sourceIp": source,
            "destinationIp": destination,
            "destinationPort": port or None,
            "protocol": protocol_number or None,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "flows": int(row.get("flows") or 0),
        "denied": int(row.get("denied") or 0),
        "accepted": int(row.get("accepted") or 0),
        "actions": list(row.get("actions") or ()),
        "sourceInterfaces": list(row.get("source_interfaces") or ()),
        "destinationInterfaces": list(row.get("destination_interfaces") or ()),
        "firstSeen": row.get("first_seen"),
        "lastSeen": row.get("last_seen"),
    }


def collect_environment_finding(finding_id: str, subject: str | None) -> dict[str, Any]:
    """Re-read one environment finding from the latest source-backed sweep."""
    from domains.network_rca.environment import build_environment_report

    report = build_environment_report()
    finding = next(
        (
            dict(item) for item in report.get("findings") or ()
            if str(item.get("finding_id") or "") == finding_id
            and (not subject or str(item.get("subject") or "") == subject)
        ),
        None,
    )
    if finding is None:
        return {
            "available": True,
            "checked_at": report.get("checked_at"),
            "finding_id": finding_id,
            "subject": subject,
            "verification": {"state": "cleared"},
        }
    measured = dict(finding.get("measured") or {})
    transitions = list(measured.get("transitions") or ())
    if len(transitions) > 24:
        measured["transitions"] = [*transitions[:4], *transitions[-20:]]
        measured["transitions_truncated"] = len(transitions) - 24
    return {
        "available": True,
        "checked_at": report.get("checked_at"),
        "finding_id": finding_id,
        "subject": finding.get("subject"),
        "fault_class": finding.get("fault_class"),
        "severity": finding.get("severity"),
        "measured": measured,
        "verification": dict(finding.get("verification") or {}),
        "evidence": dict(finding.get("evidence") or {}),
        "cannot_prove": list(finding.get("cannot_prove") or ()),
    }


def collect_admin_auth_window(
    incident_start: str | None, incident_end: str | None,
    *,
    managed_device: str | None = None,
    failure_threshold: int = 12,
    distinct_source_threshold: int = 5,
) -> dict[str, Any]:
    """Read the exact gateway authentication window from ClickHouse."""
    from .history import _CH_DB, _q

    try:
        start = datetime.fromisoformat(str(incident_start or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(incident_end or "").replace("Z", "+00:00"))
    except ValueError:
        return {"available": False, "reason": "incident_window_required"}
    device = str(managed_device or "").strip()
    if not device or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", device) is None:
        return {"available": False, "reason": "managed_device_required"}
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start_text = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    end_text = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows = _q(
        "SELECT countIf(event_type='admin_login_failed') AS failed_logins, "
        "uniqExactIf(srcip, event_type='admin_login_failed' AND srcip!='') AS distinct_sources, "
        "countIf(event_type='admin_login_lockout') AS lockouts, "
        "groupUniqArrayIf(12)(srcip, event_type='admin_login_failed' AND srcip!='') AS sources, "
        "groupUniqArrayIf(12)(username, username!='') AS usernames, "
        "min(event_ts) AS first_seen, max(event_ts) AS last_seen "
        f"FROM {_CH_DB}.security_events "
        f"WHERE event_ts >= toDateTime64('{start_text}',3) "
        f"AND event_ts <= toDateTime64('{end_text}',3) "
        f"AND (device_id='{device}' OR device_name='{device}')"
    )
    row = rows[0] if rows else {}
    return {
        "available": True,
        "failure_threshold": max(1, int(failure_threshold)),
        "distinct_source_threshold": max(1, int(distinct_source_threshold)),
        "query_scope": {
            "start": start.isoformat(), "end": end.isoformat(), "managed_device": device,
        },
        "failed_logins": int(row.get("failed_logins") or 0),
        "distinct_sources": int(row.get("distinct_sources") or 0),
        "lockouts": int(row.get("lockouts") or 0),
        "sources": list(row.get("sources") or ()),
        "usernames": list(row.get("usernames") or ()),
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
    }


__all__ = [
    "collect_case_flow_window",
    "collect_admin_auth_window",
    "collect_environment_finding",
    "collect_fortigate_context",
]
