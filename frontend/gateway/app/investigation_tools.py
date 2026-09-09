"""Read-only network evidence adapters used by a live investigation."""

from __future__ import annotations

import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor
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


def resolve_device_subject(
    question: str,
    *,
    api_factory: Callable[..., FortiGateReadonlyAPI] = FortiGateReadonlyAPI.from_env,
) -> dict[str, Any] | None:
    """Resolve a literal inventory hostname, IP or MAC mentioned in text."""
    text = question.casefold()
    api = api_factory(verify_tls=False, timeout=5.0, retries=1)
    result = api.fetch_devices()
    for item in result.get("items") or ():
        for field in ("ip", "mac", "hostname"):
            value = str(item.get(field) or "").strip()
            if not value:
                continue
            pattern = rf"(?<![\w.:-]){re.escape(value.casefold())}(?![\w.:-])"
            if re.search(pattern, text):
                resolved_ip = str(item.get("ip") or "")
                return {
                    "subject": resolved_ip if _is_ip(resolved_ip) else value,
                    "matchedField": field,
                    "matchedValue": value,
                    "source": "fortigate_device_inventory",
                    "fetchedAt": result.get("fetched_at"),
                }
    return None


def collect_fortigate_current_status(
    subject: str,
    *,
    api_factory: Callable[..., FortiGateReadonlyAPI] = FortiGateReadonlyAPI.from_env,
    device_status_reader: Callable[[str, str], dict[str, Any] | None] | None = None,
    live_flow_reader: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Narrow current-state path for one inventory identity or IP."""
    if device_status_reader is None or live_flow_reader is None:
        from .live_identity import device_status, live_flows

        device_status_reader = device_status_reader or device_status
        live_flow_reader = live_flow_reader or live_flows
    identity_item = None
    try:
        from core.memory.knowledge_sync import knowledge_sync_status

        identity_item = knowledge_sync_status().get("fingerprint")
    except Exception:
        pass
    if _is_ip(subject):
        devices = {"items": [], "degraded": False}
    else:
        devices = api_factory(
            verify_tls=False, timeout=5.0, retries=1
        ).fetch_devices()
    matched = [
        item for item in devices.get("items") or ()
        if subject.casefold() in {
            str(item.get("ip") or "").casefold(),
            str(item.get("mac") or "").casefold(),
            str(item.get("hostname") or "").casefold(),
        }
    ][:8]
    resolved_ip = next(
        (str(item.get("ip")) for item in matched if _is_ip(str(item.get("ip") or ""))),
        subject if _is_ip(subject) else None,
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    target_status = None
    target_flows = None
    if resolved_ip:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="current-status") as executor:
            status_job = executor.submit(device_status_reader, resolved_ip, "zh")
            flows_job = executor.submit(live_flow_reader, resolved_ip)
            target_status = status_job.result()
            target_flows = flows_job.result()
    return {
        "available": bool(resolved_ip or matched),
        "degraded": bool(devices.get("degraded")),
        "observedAt": observed_at,
        "sourceRole": "current_state_readback",
        "readPlan": (
            ["current_sessions"]
            if _is_ip(subject)
            else ["target_inventory", "current_sessions"]
        ),
        "managedDeviceIdentity": identity_item,
        "targetDevices": matched,
        "subjectResolution": {
            "input": subject,
            "resolvedIp": resolved_ip,
            "method": "inventory_exact_match" if matched else "ip_literal" if resolved_ip else "unresolved",
        },
        "targetStatus": target_status,
        "targetFlows": target_flows,
    }


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
    operational_reader = getattr(api, "fetch_operational_state", None)
    operational_state = operational_reader() if callable(operational_reader) else None
    devices = snapshot["devices"].get("items") or []
    identities = snapshot.get("identity", {}).get("items") or []
    target_devices = [
        item
        for item in devices
        if subject and subject.casefold() in {
            str(item.get("ip") or "").casefold(),
            str(item.get("mac") or "").casefold(),
            str(item.get("hostname") or "").casefold(),
        }
    ][:8]
    resolved_ip = next(
        (str(item.get("ip")) for item in target_devices if _is_ip(str(item.get("ip") or ""))),
        subject if _is_ip(subject) else None,
    )
    result: dict[str, Any] = {
        "fetched_at": snapshot["fetched_at"],
        "sourceRole": "current_state_readback",
        "freshness": {
            "direct": True,
            "observedAt": snapshot["fetched_at"],
        },
        "degraded": bool(snapshot.get("degraded")),
        "availability": {
            key: {
                "available": bool(snapshot[key].get("available")),
                "degraded": bool(snapshot[key].get("degraded")),
                "missing": list(snapshot[key].get("missing") or ()),
            }
            for key in ("identity", "interfaces", "devices", "policies", "changes")
            if key in snapshot
        },
        "managedDeviceIdentity": identities[0] if identities else None,
        "target_devices": target_devices,
        "subjectResolution": {
            "input": subject,
            "resolvedIp": resolved_ip,
            "method": "inventory_exact_match" if target_devices else "ip_literal" if resolved_ip else "unresolved",
        },
        "interfaces": list(snapshot["interfaces"].get("items") or ())[:64],
        "policies": list(snapshot["policies"].get("items") or ())[:128],
        "changes": list(snapshot["changes"].get("items") or ())[:20],
        "inventory_count": len(devices),
    }
    if operational_state is not None:
        result["operationalState"] = operational_state
        result["degraded"] = bool(
            result["degraded"] or operational_state.get("degraded")
        )
        result["availability"]["operational_state"] = {
            "available": bool(operational_state.get("available")),
            "degraded": bool(operational_state.get("degraded")),
            "missing": list(operational_state.get("missing") or ()),
        }
    if resolved_ip:
        result["target_status"] = device_status_reader(resolved_ip, "zh")
        result["target_flows"] = live_flow_reader(resolved_ip)
    return result


def collect_case_flow_window(
    facts: dict[str, Any],
    incident_start: str | None,
    incident_end: str | None,
) -> dict[str, Any]:
    """Return the exact ClickHouse slice around one triggering flow tuple."""
    from .history import _CH_DB, _CH_TABLE, _q

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
        "min(event_ts) AS first_seen, max(event_ts) AS last_seen, "
        "max(ingested_at) AS latest_ingested_at, "
        "max(dateDiff('millisecond', event_ts, ingested_at)) AS max_ingest_delay_ms, "
        f"(SELECT max(ingested_at) FROM {_CH_DB}.{_CH_TABLE}) AS pipeline_latest_ingested_at, "
        f"(SELECT dateDiff('millisecond', max(ingested_at), now64(3)) "
        f" FROM {_CH_DB}.{_CH_TABLE}) AS pipeline_lag_ms "
        f"FROM {_CH_DB}.{_CH_TABLE} WHERE {where}"
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
        "latestIngestedAt": row.get("latest_ingested_at"),
        "maxIngestDelayMs": int(row.get("max_ingest_delay_ms") or 0),
        "pipelineLatestIngestedAt": row.get("pipeline_latest_ingested_at"),
        "pipelineLagMs": int(row.get("pipeline_lag_ms") or 0),
        "queriedAt": datetime.now(timezone.utc).isoformat(),
        "sourceRole": "historical_event_window",
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
    expected_failure_count: int | None = None,
    expected_distinct_sources: int | None = None,
    source_event_ids: list[str] | tuple[str, ...] = (),
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
    stable_event_ids = [
        str(value) for value in source_event_ids
        if re.fullmatch(r"[A-Fa-f0-9]{64}", str(value))
    ][:64]
    source_id_expression = (
        "countIf(event_id IN ("
        + ",".join(f"'{value}'" for value in stable_event_ids)
        + ")) AS archived_source_events, "
        if stable_event_ids else "0 AS archived_source_events, "
    )
    rows = _q(
        "SELECT countIf(event_type='admin_login_failed') AS failed_logins, "
        "uniqExactIf(srcip, event_type='admin_login_failed' AND srcip!='') AS distinct_sources, "
        "countIf(event_type='admin_login_lockout') AS lockouts, "
        "groupUniqArrayIf(12)(srcip, event_type='admin_login_failed' AND srcip!='') AS sources, "
        "groupUniqArrayIf(12)(username, username!='') AS usernames, "
        "min(event_ts) AS first_seen, max(event_ts) AS last_seen, "
        "max(ingested_at) AS latest_ingested_at, "
        "max(dateDiff('millisecond', event_ts, ingested_at)) AS max_ingest_delay_ms, "
        + source_id_expression +
        f"(SELECT max(ingested_at) FROM {_CH_DB}.security_events) AS pipeline_latest_ingested_at, "
        f"(SELECT dateDiff('millisecond', max(ingested_at), now64(3)) "
        f" FROM {_CH_DB}.security_events) AS pipeline_lag_ms "
        f"FROM {_CH_DB}.security_events "
        f"WHERE event_ts >= toDateTime64('{start_text}',3) "
        f"AND event_ts <= toDateTime64('{end_text}',3) "
        f"AND (device_id='{device}' OR device_name='{device}')"
    )
    row = rows[0] if rows else {}
    failures = int(row.get("failed_logins") or 0)
    lockouts = int(row.get("lockouts") or 0)
    distinct = int(row.get("distinct_sources") or 0)
    expected_count = (
        max(0, int(expected_failure_count))
        if expected_failure_count is not None else None
    )
    expected_distinct = (
        max(0, int(expected_distinct_sources))
        if expected_distinct_sources is not None else None
    )
    observed_total = failures + lockouts
    threshold_triggered = bool(
        expected_count is not None
        and expected_distinct is not None
        and expected_count >= max(1, int(failure_threshold))
        and expected_distinct >= max(1, int(distinct_source_threshold))
    )
    archive_reproduced = bool(
        observed_total >= max(1, int(failure_threshold))
        and distinct >= max(1, int(distinct_source_threshold))
    ) or lockouts > 0
    id_coverage = (
        int(row.get("archived_source_events") or 0) / len(stable_event_ids)
        if stable_event_ids else None
    )
    consistency_state = (
        "conflict" if threshold_triggered and not archive_reproduced
        else "consistent"
    )
    return {
        "available": True,
        "failure_threshold": max(1, int(failure_threshold)),
        "distinct_source_threshold": max(1, int(distinct_source_threshold)),
        "query_scope": {
            "start": start.isoformat(), "end": end.isoformat(), "managed_device": device,
        },
        "failed_logins": failures,
        "distinct_sources": distinct,
        "lockouts": lockouts,
        "sources": list(row.get("sources") or ()),
        "usernames": list(row.get("usernames") or ()),
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "latest_ingested_at": row.get("latest_ingested_at"),
        "max_ingest_delay_ms": int(row.get("max_ingest_delay_ms") or 0),
        "pipeline_latest_ingested_at": row.get("pipeline_latest_ingested_at"),
        "pipeline_lag_ms": int(row.get("pipeline_lag_ms") or 0),
        "evidence_consistency": {
            "state": consistency_state,
            "trigger_failure_count": expected_count,
            "trigger_distinct_sources": expected_distinct,
            "archive_failure_or_lockout_count": observed_total,
            "archive_distinct_sources": distinct,
            "source_event_ids_supplied": len(stable_event_ids),
            "source_event_ids_archived": int(row.get("archived_source_events") or 0),
            "source_event_id_coverage": id_coverage,
        },
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "source_role": "historical_event_window",
    }


__all__ = [
    "collect_case_flow_window",
    "collect_admin_auth_window",
    "collect_environment_finding",
    "collect_fortigate_context",
    "collect_fortigate_current_status",
    "resolve_device_subject",
]
