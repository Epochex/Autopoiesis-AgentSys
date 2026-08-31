"""Source-backed projection for the production situational-awareness page.

The page contract is deliberately smaller than the investigation APIs.  It
answers five operator questions with current sources: which assets are visible,
what changed, what crossed a policy boundary, what is affected, and which case
or read-only probe owns the next step.  Historical replay fixtures never enter
this projection.
"""
from __future__ import annotations

import ipaddress
import json
import os
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from domains.network_rca.fortigate_api import FortiGateReadonlyAPI

from .history import _CH_DB, _q
from .investigation_cases import is_observed_case


_CACHE_TTL_SECONDS = 20.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None
_DEFAULT_ROUTER_FACTORY = FortiGateReadonlyAPI.from_env

_FAULT_TITLES = {
    "duplicate_ip_static": "地址归属与租约冲突已由复核确认",
    "duplicate_ip_dhcp": "同一地址出现多个 DHCP 归属",
    "address_unmanaged": "活跃地址缺少租约或资产归属",
    "host_multi_address": "同一设备在短时间内持有多个地址",
    "lease_churn": "设备租约持续重建",
    "pool_pressure": "地址池剩余容量进入风险区间",
    "session_tuple_clash": "同一会话标识出现冲突",
    "unmanaged_identity": "设备身份无法关联现有资产记录",
    "mgmt_bruteforce": "管理入口遭遇连续认证尝试",
}


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _finding_title(finding: dict[str, Any]) -> str:
    fault = str(finding.get("fault_class") or "")
    subject = str(finding.get("subject") or "未知对象")
    title = _FAULT_TITLES.get(fault)
    return f"{subject} · {title}" if title else str(
        finding.get("headline") or fault or "环境状态发生变化"
    )


def _private_ip(value: Any) -> ipaddress.IPv4Address | None:
    try:
        parsed = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return None
    if not isinstance(parsed, ipaddress.IPv4Address) or not parsed.is_private:
        return None
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_reserved:
        return None
    if parsed == ipaddress.IPv4Address("255.255.255.255"):
        return None
    return parsed


def _public_ip(value: Any) -> bool:
    try:
        parsed = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return False
    return not (
        parsed.is_private or parsed.is_loopback or parsed.is_link_local
        or parsed.is_multicast or parsed.is_unspecified or parsed.is_reserved
    )


def _network_from_interface(item: dict[str, Any]) -> ipaddress.IPv4Network | None:
    value = str(item.get("ip") or "").strip()
    if not value:
        return None
    parts = value.split()
    candidate = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else value
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        return None
    return network if isinstance(network, ipaddress.IPv4Network) else None


def _segment_catalog(interfaces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in interfaces:
        network = _network_from_interface(item)
        if (
            network is None or not network.is_private or network.prefixlen < 8
            or network.network_address.is_unspecified or str(network) in seen
        ):
            continue
        seen.add(str(network))
        catalog.append({
            "id": str(network),
            "cidr": str(network),
            "name": str(item.get("name") or network),
            "role": str(item.get("role") or "unknown"),
            "interfaceStatus": str(item.get("status") or "unknown"),
            "vlanId": _number(item.get("vlan_id")) or None,
            "network": network,
        })
    catalog.sort(key=lambda item: item["network"].prefixlen, reverse=True)
    return catalog


def _segment_for(ip: str, catalog: list[dict[str, Any]]) -> str:
    parsed = _private_ip(ip)
    if parsed is None:
        return "external"
    for item in catalog:
        if parsed in item["network"]:
            return str(item["cidr"])
    return "unmapped-private"


def _safe_query(query: Callable[[str], list[dict[str, Any]]], sql: str) -> list[dict[str, Any]]:
    try:
        return list(query(sql) or ())
    except Exception:
        return []


def _router_snapshot(
    factory: Callable[..., FortiGateReadonlyAPI],
) -> dict[str, Any]:
    try:
        return factory(verify_tls=False, timeout=5.0, retries=1).fetch_all()
    except Exception as error:
        return {
            "fetched_at": None,
            "degraded": True,
            "error": type(error).__name__,
            **{
                key: {"available": False, "degraded": True, "items": [], "missing": []}
                for key in ("interfaces", "devices", "policies", "changes")
            },
        }


def _latest_facts(query: Callable[[str], list[dict[str, Any]]]) -> dict[str, Any]:
    rows = _safe_query(
        query,
        f"SELECT toString(max(event_ts)) AS latest, count() AS retained "
        f"FROM {_CH_DB}.facts",
    )
    row = rows[0] if rows else {}
    return {"latest": row.get("latest"), "retained": _number(row.get("retained"))}


def _asset_activity(query: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _safe_query(
        query,
        "WITH (SELECT max(event_ts) FROM {db}.facts) AS anchor "
        "SELECT srcip, anyLast(srcname) AS srcname, anyLast(srcintf) AS srcintf, "
        "count() AS flows, sum(sentbyte + rcvdbyte) AS bytes, "
        "countIf(action='deny') AS denied, uniqExact(dstip) AS peers, "
        "groupUniqArray(8)(service) AS services, toString(max(event_ts)) AS last_seen "
        "FROM {db}.facts WHERE event_ts >= anchor - INTERVAL 24 HOUR AND srcip != '' "
        "GROUP BY srcip ORDER BY last_seen DESC LIMIT 2000".format(db=_CH_DB),
    )


def _behavior_rows(query: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _safe_query(
        query,
        "WITH (SELECT max(event_ts) FROM {db}.facts) AS anchor "
        "SELECT srcip, "
        "countIf(event_ts >= anchor - INTERVAL 24 HOUR) AS current_flows, "
        "countIf(event_ts >= anchor - INTERVAL 24 HOUR AND action='deny') AS current_denied, "
        "uniqExactIf(dstip, event_ts >= anchor - INTERVAL 24 HOUR AND dstip!='') AS current_peers, "
        "uniqExactIf(dstport, event_ts >= anchor - INTERVAL 24 HOUR AND dstport>0) AS current_ports, "
        "countIf(event_ts < anchor - INTERVAL 24 HOUR) / 7.0 AS baseline_flows, "
        "countIf(event_ts < anchor - INTERVAL 24 HOUR AND action='deny') / 7.0 AS baseline_denied, "
        "uniqExactIf(dstip, event_ts < anchor - INTERVAL 24 HOUR AND dstip!='') / 7.0 AS baseline_peers, "
        "toString(min(event_ts)) AS first_seen "
        "FROM {db}.facts WHERE event_ts >= anchor - INTERVAL 8 DAY AND srcip != '' "
        "GROUP BY srcip HAVING current_flows > 0 ORDER BY current_flows DESC LIMIT 2000".format(
            db=_CH_DB
        ),
    )


def _cross_segment_rows(query: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _safe_query(
        query,
        "WITH (SELECT max(event_ts) FROM {db}.facts) AS anchor "
        "SELECT srcip, dstip, action, srcintf, dstintf, service, dstport, "
        "count() AS flows, sum(sentbyte + rcvdbyte) AS bytes, "
        "toString(min(event_ts)) AS first_seen, toString(max(event_ts)) AS last_seen "
        "FROM {db}.facts WHERE event_ts >= anchor - INTERVAL 24 HOUR "
        "AND srcip != '' AND dstip != '' "
        "GROUP BY srcip,dstip,action,srcintf,dstintf,service,dstport "
        "ORDER BY flows DESC LIMIT 2000".format(db=_CH_DB),
    )


def _external_sources(query: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = _safe_query(
        query,
        "WITH (SELECT max(event_ts) FROM {db}.security_events WHERE provenance='real') AS anchor "
        "SELECT srcip, count() AS events, uniqExact(event_id) AS unique_events, "
        "groupUniqArray(8)(event_type) AS event_types, groupUniqArray(8)(dstport) AS ports, "
        "toString(max(event_ts)) AS last_seen FROM {db}.security_events "
        "WHERE provenance='real' AND event_ts >= anchor - INTERVAL 24 HOUR AND srcip != '' "
        "GROUP BY srcip ORDER BY unique_events DESC LIMIT 500".format(db=_CH_DB),
    )
    return [row for row in rows if _public_ip(row.get("srcip"))]


def _funnel(query: Callable[[str], list[dict[str, Any]]]) -> dict[str, int]:
    rows = _safe_query(
        query,
        "SELECT "
        "(SELECT count() FROM {db}.facts WHERE event_ts >= now() - INTERVAL 24 HOUR) AS facts, "
        "(SELECT uniqExact(event_id) FROM {db}.security_events WHERE provenance='real' "
        " AND event_ts >= now() - INTERVAL 24 HOUR) AS security_events, "
        "(SELECT uniqExact(alert_id) FROM {db}.alerts WHERE alert_ts >= now() - INTERVAL 24 HOUR) AS alerts".format(
            db=_CH_DB
        ),
    )
    row = rows[0] if rows else {}
    return {key: _number(row.get(key)) for key in ("facts", "security_events", "alerts")}


def _load_intel() -> tuple[dict[str, dict[str, Any]], str | None]:
    path = Path(os.getenv(
        "AUTOPOIESIS_INTEL_FEED",
        "/data/autopoiesis-production/intel/indicators.jsonl",
    ))
    indicators: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, None
    for line in lines[-100_000:]:
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        indicator = str(item.get("indicator") or item.get("ip") or "").strip()
        if indicator:
            indicators[indicator] = {
                "source": str(item.get("source") or path.name),
                "label": str(item.get("label") or item.get("threat") or "external indicator"),
                "updatedAt": item.get("updated_at") or item.get("updatedAt"),
            }
    return indicators, str(path)


def _case_projection(repository: Any, now: datetime) -> tuple[list[Any], list[dict[str, Any]]]:
    cutoff = now - timedelta(hours=48)
    cases: list[Any] = []
    projected: list[dict[str, Any]] = []
    for case in repository.list(limit=500):
        last_seen = _utc(case.last_seen_at)
        if last_seen is None or last_seen < cutoff or not is_observed_case(case):
            continue
        decision_event = case.latest_event("business_decision_recorded") or {}
        decision = dict(decision_event.get("decision") or {})
        if decision.get("classification") == "routine_observation_suppressed":
            continue
        if case.severity.casefold() not in {"high", "critical", "error"} and not decision:
            continue
        cases.append(case)
        evidence = [item for item in case.timeline if item.get("kind") == "evidence_collected"]
        readback = decision.get("readback") or (
            case.latest_event("remediation_completed") or {}
        ).get("readback")
        projected.append({
            "caseId": case.case_id,
            "status": case.status,
            "severity": case.severity,
            "subject": case.subject,
            "service": case.service,
            "title": str(decision.get("headline") or case.summary),
            "summary": str(decision.get("summary") or case.summary),
            "lastSeenAt": case.last_seen_at,
            "occurrences": case.occurrence_count,
            "evidenceCount": len(evidence) + len(list(decision.get("evidence") or ())),
            "action": str(decision.get("action") or ""),
            "readback": readback,
            "classification": str(decision.get("classification") or "investigating"),
        })
    projected.sort(key=lambda item: str(item["lastSeenAt"]), reverse=True)
    return cases, projected


def _effect_measurement(cases: list[Any]) -> dict[str, Any]:
    decision_seconds: list[float] = []
    closure_seconds: list[float] = []
    recurrence_groups: dict[str, list[Any]] = defaultdict(list)
    for case in cases:
        first = _utc(case.first_seen_at)
        if first is None:
            continue
        decision = case.latest_event("business_decision_recorded")
        decision_at = _utc(str((decision or {}).get("ts") or ""))
        if decision_at and decision_at >= first:
            decision_seconds.append((decision_at - first).total_seconds())
        remediation = case.latest_event("remediation_completed")
        closed_at = _utc(str((remediation or {}).get("ts") or ""))
        if closed_at and closed_at >= first:
            closure_seconds.append((closed_at - first).total_seconds())
        # This projection cannot consult the replay topology.  A production
        # recurrence cohort is the same observed subject, detector and service.
        recurrence_key = "\0".join((
            str(case.subject or ""), str(case.rule_id or ""), str(case.service or ""),
        ))
        recurrence_groups[recurrence_key].append(case)
    real_pairs = sum(1 for group in recurrence_groups.values() if len(group) >= 2)
    qualified = len(decision_seconds) >= 20 and real_pairs >= 5
    return {
        "qualified": qualified,
        "completedInvestigations": len(decision_seconds),
        "recurrenceCohorts": real_pairs,
        "medianDecisionSeconds": (
            round(statistics.median(decision_seconds), 1) if qualified else None
        ),
        "medianClosureSeconds": (
            round(statistics.median(closure_seconds), 1) if qualified and closure_seconds else None
        ),
        "minimumForComparison": {"investigations": 20, "recurrenceCohorts": 5},
    }


def _build(
    repository: Any,
    environment_report: dict[str, Any] | None,
    *,
    query: Callable[[str], list[dict[str, Any]]],
    router_factory: Callable[..., FortiGateReadonlyAPI],
    now: datetime,
) -> dict[str, Any]:
    router = _router_snapshot(router_factory)
    interfaces = list(router.get("interfaces", {}).get("items") or ())
    devices = list(router.get("devices", {}).get("items") or ())
    policies = list(router.get("policies", {}).get("items") or ())
    catalog = _segment_catalog(interfaces)
    latest = _latest_facts(query)
    activity_rows = _asset_activity(query)
    behavior_rows = _behavior_rows(query)
    cross_rows = _cross_segment_rows(query)
    external_rows = _external_sources(query)
    funnel = _funnel(query)
    cases, case_rows = _case_projection(repository, now)

    device_by_ip: dict[str, dict[str, Any]] = {}
    for item in devices:
        ip = str(item.get("ip") or "")
        if _private_ip(ip) is None:
            continue
        existing = device_by_ip.setdefault(ip, {"ip": ip, "sources": []})
        for field in ("mac", "hostname"):
            if item.get(field) and not existing.get(field):
                existing[field] = item[field]
        existing["sources"] = sorted(set(existing["sources"] + list(item.get("sources") or ())))

    active_by_ip: dict[str, dict[str, Any]] = {}
    for row in activity_rows:
        ip = str(row.get("srcip") or "")
        if _private_ip(ip) is None:
            continue
        active_by_ip[ip] = {
            "flows24h": _number(row.get("flows")),
            "bytes24h": _number(row.get("bytes")),
            "denied24h": _number(row.get("denied")),
            "peers24h": _number(row.get("peers")),
            "lastSeenAt": row.get("last_seen"),
            "sourceInterface": str(row.get("srcintf") or ""),
            "observedOutboundServices": [
                str(value) for value in row.get("services") or () if value
            ],
        }

    segment_counts: dict[str, Counter] = defaultdict(Counter)
    for ip in set(device_by_ip) | set(active_by_ip):
        segment = _segment_for(ip, catalog)
        segment_counts[segment]["assets"] += 1
        if ip in active_by_ip:
            segment_counts[segment]["active"] += 1
    segments = []
    for item in catalog:
        counts = segment_counts.get(str(item["cidr"]), Counter())
        segments.append({
            key: value for key, value in item.items() if key != "network"
        } | {"assetCount": counts["assets"], "active24h": counts["active"]})
    if segment_counts.get("unmapped-private"):
        counts = segment_counts["unmapped-private"]
        segments.append({
            "id": "unmapped-private", "cidr": "unmapped-private", "name": "未映射私网地址",
            "role": "unknown", "interfaceStatus": "unknown", "vlanId": None,
            "assetCount": counts["assets"], "active24h": counts["active"],
        })
    segments.sort(key=lambda item: (item["active24h"], item["assetCount"]), reverse=True)

    behaviors: list[dict[str, Any]] = []
    behavior_by_ip: dict[str, dict[str, Any]] = {}
    for row in behavior_rows:
        current = float(row.get("current_flows") or 0)
        baseline = float(row.get("baseline_flows") or 0)
        denied = float(row.get("current_denied") or 0)
        base_denied = float(row.get("baseline_denied") or 0)
        peers = float(row.get("current_peers") or 0)
        base_peers = float(row.get("baseline_peers") or 0)
        reasons: list[str] = []
        ip = str(row.get("srcip") or "")
        if _private_ip(ip) is None:
            continue
        ratio = current / max(1.0, baseline)
        if baseline >= 20 and current >= 100 and ratio >= 2.5:
            reasons.append(f"24 小时活动量达到过去 7 天日均的 {ratio:.1f} 倍")
        deny_ratio = denied / max(1.0, current)
        base_deny_ratio = base_denied / max(1.0, baseline)
        if baseline >= 20 and current >= 20 and deny_ratio >= 0.8 and deny_ratio >= base_deny_ratio + 0.2:
            reasons.append(f"拒绝占比 {deny_ratio * 100:.0f}%，高于历史日均")
        if baseline < 20 and current >= 500 and deny_ratio >= 0.95:
            reasons.append(f"过去 7 天缺少活动基线，24 小时内新增 {int(denied)} 条被拒绝记录")
        if peers >= 8 and peers >= max(8.0, base_peers * 2.5):
            reasons.append(f"访问 {int(peers)} 个目标，高于过去 7 天日均")
        if not reasons:
            continue
        severity = "high" if ratio >= 5 or deny_ratio >= 0.95 else "medium"
        anomaly = {
            "id": f"behavior:{ip}", "asset": ip, "segment": _segment_for(ip, catalog),
            "severity": severity, "kind": "device_behavior_deviation", "reasons": reasons,
            "current": {"flows": int(current), "denied": int(denied), "peers": int(peers),
                        "ports": _number(row.get("current_ports"))},
            "baselineDaily": {"flows": round(baseline, 1), "denied": round(base_denied, 1),
                              "peers": round(base_peers, 1)},
            "evidence": {
                "source": f"{_CH_DB}.facts", "window": "latest 24h vs previous 7d",
                "firstSeenAt": row.get("first_seen"),
            },
        }
        behaviors.append(anomaly)
        behavior_by_ip[ip] = anomaly
    behaviors.sort(key=lambda item: (item["severity"] == "high", item["current"]["flows"]), reverse=True)

    cross_segment: list[dict[str, Any]] = []
    for row in cross_rows:
        source = str(row.get("srcip") or "")
        destination = str(row.get("dstip") or "")
        if _private_ip(source) is None or _private_ip(destination) is None:
            continue
        source_segment = _segment_for(source, catalog)
        destination_segment = _segment_for(destination, catalog)
        if source_segment == destination_segment:
            continue
        action = str(row.get("action") or "unknown").casefold()
        cross_segment.append({
            "id": f"boundary:{source}:{destination}:{row.get('dstport')}:{action}",
            "source": source, "destination": destination,
            "sourceSegment": source_segment, "destinationSegment": destination_segment,
            "service": str(row.get("service") or ""), "port": _number(row.get("dstport")) or None,
            "action": action, "flows": _number(row.get("flows")), "bytes": _number(row.get("bytes")),
            "firstSeenAt": row.get("first_seen"), "lastSeenAt": row.get("last_seen"),
            "sourceInterface": str(row.get("srcintf") or ""),
            "destinationInterface": str(row.get("dstintf") or ""),
        })
    cross_segment.sort(key=lambda item: item["flows"], reverse=True)
    cross_segment = cross_segment[:30]

    environment_findings = [
        dict(item) for item in (environment_report or {}).get("findings") or ()
        if str(dict(item).get("verification", {}).get("state") or "") == "confirmed"
    ]
    environment_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in environment_findings:
        environment_by_subject[str(finding.get("subject") or "")].append(finding)
    cases_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in case_rows:
        cases_by_subject[str(case["subject"])].append(case)

    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    risk_assets: list[dict[str, Any]] = []
    for ip in set(environment_by_subject) | set(behavior_by_ip) | set(cases_by_subject):
        reasons: list[str] = []
        severity = "low"
        for finding in environment_by_subject.get(ip, ()):
            candidate = str(finding.get("severity") or "medium").casefold()
            if severity_order.get(candidate, 0) > severity_order.get(severity, 0):
                severity = candidate
            reasons.append(_finding_title(finding))
        if ip in behavior_by_ip:
            candidate = str(behavior_by_ip[ip]["severity"])
            if severity_order.get(candidate, 0) > severity_order.get(severity, 0):
                severity = candidate
            reasons.extend(behavior_by_ip[ip]["reasons"])
        active_cases = cases_by_subject.get(ip, ())
        if any(item["status"] in {"open", "investigating", "escalated"} for item in active_cases):
            severity = "critical" if any(item["severity"] == "critical" for item in active_cases) else max(
                severity, "high", key=lambda value: severity_order.get(value, 0)
            )
            reasons.append("存在尚未关闭的生产调查")
        identity = device_by_ip.get(ip, {})
        segment = _segment_for(ip, catalog) if _private_ip(ip) else "managed-device"
        risk_assets.append({
            "asset": ip, "name": identity.get("hostname") or ip,
            "mac": identity.get("mac"), "segment": segment,
            "severity": severity, "reasons": list(dict.fromkeys(reasons))[:5],
            "caseIds": [item["caseId"] for item in active_cases],
            "activity": active_by_ip.get(ip),
        })
    risk_assets.sort(key=lambda item: (
        severity_order.get(str(item["severity"]), 0),
        _number(dict(item.get("activity") or {}).get("flows24h")),
    ), reverse=True)

    policy_directions = {
        (tuple(item.get("source_zones") or ()), tuple(item.get("destination_zones") or ())): item
        for item in policies
    }
    paths: list[dict[str, Any]] = []
    for record in cross_segment[:12]:
        matching = [
            item for (sources, destinations), item in policy_directions.items()
            if record["sourceInterface"] in sources and record["destinationInterface"] in destinations
        ]
        action = record["action"]
        state = "blocked" if action in {"deny", "blocked", "reject"} else "observed_allowed"
        paths.append({
            "id": f"path:{record['id']}", "state": state,
            "label": "已阻断的跨区访问" if state == "blocked" else "已观测的跨区访问",
            "steps": [
                {"kind": "asset", "label": record["source"], "segment": record["sourceSegment"]},
                {"kind": "policy", "label": (
                    f"策略 {matching[0]['id']} · {matching[0]['action']}" if matching
                    else f"设备记录 · {record['action']}"
                )},
                {"kind": "service", "label": (
                    f"{record['destination']}:{record['port']}" if record["port"] else record["destination"]
                ), "segment": record["destinationSegment"]},
            ],
            "flows": record["flows"], "lastSeenAt": record["lastSeenAt"],
            "evidence": {"source": f"{_CH_DB}.facts", "action": record["action"]},
        })

    intel, intel_path = _load_intel()
    external_sources = []
    for row in external_rows[:50]:
        ip = str(row.get("srcip") or "")
        match = intel.get(ip)
        external_sources.append({
            "ip": ip, "events": _number(row.get("unique_events")),
            "eventTypes": list(row.get("event_types") or ()),
            "ports": [_number(value) for value in row.get("ports") or () if _number(value)],
            "lastSeenAt": row.get("last_seen"), "intelMatch": match,
        })

    status_counts = Counter(case.status for case in cases)
    funnel.update({
        "cases": len(cases),
        "investigating": status_counts["open"] + status_counts["investigating"],
        "escalated": status_counts["escalated"],
        "resolved": status_counts["resolved"],
        "actionsVerified": sum(1 for case in case_rows if case.get("readback")),
    })

    latest_at = _utc(str(latest.get("latest") or ""))
    lag = max(0, int((now - latest_at).total_seconds())) if latest_at else None
    has_allowed_cross_segment = any(
        item["action"] in {"accept", "allow", "pass", "close"} for item in cross_segment
    )
    coverage = [
        {"capability": "asset_inventory", "state": "covered", "label": "受管设备与活跃地址"},
        {"capability": "cross_segment_denies", "state": "covered", "label": "跨网段拒绝记录"},
        {"capability": "accepted_cross_segment", "state": "covered" if has_allowed_cross_segment else "blind",
         "label": "跨网段成功连接", "requires": None if has_allowed_cross_segment else "在 FortiGate 开启 accept 会话日志"},
        {"capability": "same_segment_movement", "state": "blind", "label": "同网段横向通信",
         "requires": "交换机流量镜像、NetFlow 或端点网络探针"},
        {"capability": "user_behavior", "state": "blind", "label": "用户身份行为",
         "requires": "身份系统、VPN 或终端登录数据"},
        {"capability": "endpoint_process_chain", "state": "blind", "label": "终端进程与文件攻击链",
         "requires": "端点检测或主机审计数据"},
        {"capability": "switch_port_location", "state": "blind", "label": "MAC 对应交换机端口",
         "requires": "FortiSwitch MAC 地址表"},
        {"capability": "external_intelligence", "state": "covered" if intel_path else "blind",
         "label": "外部威胁来源匹配", "requires": None if intel_path else "已授权的本地情报订阅文件"},
    ]

    changes = []
    for finding in environment_findings:
        changes.append({
            "id": str(finding.get("finding_id") or ""),
            "at": str(dict(finding.get("verification") or {}).get("checked_at") or (environment_report or {}).get("checked_at") or ""),
            "severity": str(finding.get("severity") or "medium"),
            "asset": str(finding.get("subject") or ""),
            "kind": str(finding.get("fault_class") or "environment_change"),
            "title": _finding_title(finding),
            "evidenceSource": str(dict(finding.get("verification") or {}).get("source") or ""),
            "caseIds": [item["caseId"] for item in cases_by_subject.get(str(finding.get("subject") or ""), ())],
        })
    for item in behaviors:
        changes.append({
            "id": item["id"], "at": latest.get("latest"), "severity": item["severity"],
            "asset": item["asset"], "kind": item["kind"], "title": item["reasons"][0],
            "evidenceSource": item["evidence"]["source"],
            "caseIds": [case["caseId"] for case in cases_by_subject.get(item["asset"], ())],
        })
    for item in external_sources:
        event_types = set(item["eventTypes"])
        if "management_exposure" in event_types:
            severity = "critical"
            title = f"公网来源 {item['ip']} 与管理端口形成成功会话记录"
        elif "admin_login_failed" in event_types:
            severity = "high"
            title = f"公网来源 {item['ip']} 持续尝试管理登录"
        else:
            severity = "medium"
            title = f"公网来源 {item['ip']} 探测管理端口"
        changes.append({
            "id": f"external:{item['ip']}", "at": item["lastSeenAt"],
            "severity": severity, "asset": item["ip"],
            "kind": "external_management_activity", "title": title,
            "evidenceSource": f"{_CH_DB}.security_events", "caseIds": [],
        })
    changes.sort(
        key=lambda item: (_utc(str(item["at"])) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        reverse=True,
    )

    asset_rows = []
    for ip in set(device_by_ip) | set(active_by_ip):
        identity = device_by_ip.get(ip, {})
        activity = active_by_ip.get(ip)
        asset_rows.append({
            "ip": ip, "mac": identity.get("mac"), "name": identity.get("hostname") or ip,
            "segment": _segment_for(ip, catalog), "sources": identity.get("sources", []),
            "active24h": activity is not None, "activity": activity,
            "risk": next((item["severity"] for item in risk_assets if item["asset"] == ip), None),
        })
    asset_rows.sort(key=lambda item: (
        item["risk"] is not None, item["active24h"], str(dict(item.get("activity") or {}).get("lastSeenAt") or "")
    ), reverse=True)

    return {
        "ok": True,
        "mode": "production_observed",
        "observedAt": now.isoformat(),
        "freshness": {
            "latestFactAt": latest.get("latest"), "lagSeconds": lag,
            "routerFetchedAt": router.get("fetched_at"),
            "routerDegraded": bool(router.get("degraded")),
        },
        "inventory": {
            "knownAssets": len(set(device_by_ip) | set(active_by_ip)),
            "active24h": len(active_by_ip), "segments": segments,
            "assets": asset_rows[:300],
        },
        "changes": changes[:30],
        "behaviorDeviations": behaviors[:30],
        "crossSegment": {
            "records": cross_segment,
            "acceptedVisible": has_allowed_cross_segment,
            "sameSegmentVisible": False,
        },
        "riskFusion": risk_assets[:30],
        "candidatePaths": paths,
        "externalSources": external_sources,
        "intelFeed": {
            "configured": bool(intel_path),
            "matches": sum(1 for item in external_sources if item["intelMatch"]),
        },
        "cases": case_rows[:30],
        "funnel": funnel,
        "effectMeasurement": _effect_measurement(cases),
        "coverage": coverage,
        "router": {
            "interfaces": len(interfaces), "policies": len(policies),
            "devices": len(devices), "configChanges": list(router.get("changes", {}).get("items") or ())[:20],
        },
    }


def build_situational_overview(
    repository: Any,
    environment_report: dict[str, Any] | None = None,
    *,
    refresh: bool = False,
    query: Callable[[str], list[dict[str, Any]]] = _q,
    router_factory: Callable[..., FortiGateReadonlyAPI] = _DEFAULT_ROUTER_FACTORY,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build and short-cache the production page contract."""
    global _cache
    use_cache = query is _q and router_factory is _DEFAULT_ROUTER_FACTORY and now is None
    if use_cache and not refresh:
        with _cache_lock:
            if _cache and time.monotonic() - _cache[0] < _CACHE_TTL_SECONDS:
                return _cache[1]
    result = _build(
        repository,
        environment_report,
        query=query,
        router_factory=router_factory,
        now=now or datetime.now(timezone.utc),
    )
    if use_cache:
        with _cache_lock:
            _cache = (time.monotonic(), result)
    return result


__all__ = ["build_situational_overview"]
