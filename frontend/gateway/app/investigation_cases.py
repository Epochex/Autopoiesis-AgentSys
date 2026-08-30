"""Join landed live-situation records to durable investigation cases."""
from __future__ import annotations

import os
import re
import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from domains.network_rca.investigation_case import (
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)


_IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _managed_gateway() -> str:
    raw = os.getenv("FGT_BASE", "https://192.168.1.1")
    return urlparse(raw).hostname or "192.168.1.1"


def _is_unmanaged_internet_ip(value: str | None) -> bool:
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local)


def _case_family(rule_id: str, service: str, summary: str) -> str | None:
    text = " ".join((rule_id, service, summary)).casefold()
    if any(marker in text for marker in ("deny", "policy", "firewall", "拒绝", "策略")):
        return "fam-policy-reachability"
    if any(marker in text for marker in ("arp", "dhcp", "address", "mac", "地址")):
        return "fam-address-ownership"
    if any(marker in text for marker in ("service", "health", "cpu", "memory", "服务", "内存")):
        return "fam-perception-selfheal"
    if any(marker in text for marker in ("exposure", "port", "listen", "暴露", "端口")):
        return "fam-exposure"
    return None


def derive_investigation_scope(case: Any) -> dict[str, Any]:
    """Translate one durable case into the exact scope used by retrieval and probes."""
    payload_text = str(case.source_payload)
    identifiers = [
        case.subject,
        *_IP.findall(" ".join((case.summary, payload_text))),
        *_MAC.findall(" ".join((case.summary, payload_text))),
    ]
    asset_ids = list(dict.fromkeys(value for value in identifiers if value))[:32]
    family = _case_family(case.rule_id, case.service, case.summary)
    subject = case.subject or None
    # Internet senders in a deny burst are evidence subjects, not managed probe
    # targets. The current firewall is the managed entity whose policy and
    # interface state can be inspected safely.
    if family == "fam-policy-reachability" and (
        not subject
        or subject.casefold() in {"r230", "fortigate", "gateway"}
        or _is_unmanaged_internet_ip(subject)
    ):
        subject = _managed_gateway()
    if subject and subject not in asset_ids:
        asset_ids.insert(0, subject)
    first = _at(case.first_seen_at) - timedelta(minutes=10)
    last = _at(case.last_seen_at) + timedelta(minutes=10)
    question = case.summary.strip() or case.title.strip() or "调查这起网络事件的根因"
    return {
        "question": question,
        "family": family,
        "subject": subject,
        "asset_ids": asset_ids,
        "incident_start": first.isoformat(),
        "incident_end": last.isoformat(),
        "source_refs": [
            {"kind": ref.kind, "source_id": ref.source_id}
            for ref in case.sources
        ],
    }


def auto_start_pending_cases(
    repository: InvestigationCaseRepository,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Start bounded read-only investigations for newly landed actionable cases."""
    from .investigate import start

    started: list[dict[str, Any]] = []
    max_age = max(30.0, float(os.getenv(
        "AUTOPOIESIS_AUTO_INVESTIGATE_MAX_AGE_SECONDS", "300"
    )))
    now = datetime.now(timezone.utc)
    for case in repository.list(status="open", limit=max(1, limit * 4)):
        try:
            age_seconds = (now - _at(case.last_seen_at)).total_seconds()
        except ValueError:
            continue
        if age_seconds < -60 or age_seconds > max_age:
            continue
        actionable = bool(case.latest_suggestion_id) or case.severity.casefold() in {
            "high", "critical", "error",
        }
        if not actionable:
            continue
        scope = derive_investigation_scope(case)
        started.append(start(
            scope["question"],
            scope["family"],
            scope["subject"],
            case.case_id,
            auto_started=True,
        ))
        if len(started) >= limit:
            break
    return started


def _alert_id(feed_item: dict[str, Any]) -> str:
    explicit = str(feed_item.get("sourceId") or "").strip()
    if explicit:
        return explicit
    value = str(feed_item.get("id") or "")
    return value.removeprefix("feed-alert-").strip()


def _suggestion_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def sync_snapshot_cases(
    snapshot: dict[str, Any], repository: InvestigationCaseRepository
) -> dict[str, Any]:
    """Persist one poll's records and annotate the response with stable case ids.

    Alerts are ingested first.  A correlated suggestion then names its sample alert
    ids and transactionally merges their provisional cases into one investigation.
    Finally every visible row resolves through the source index, so the UI never has
    to reproduce the grouping rule.
    """
    for item in snapshot.get("feed") or []:
        if item.get("kind") != "alert":
            continue
        source_id = _alert_id(item)
        occurred_at = str(item.get("ts") or "").strip()
        if not source_id or not occurred_at:
            continue
        repository.ingest(CaseObservation(
            source=SourceReference("alert", source_id),
            occurred_at=occurred_at,
            severity=str(item.get("severity") or ""),
            subject=str(item.get("deviceKey") or item.get("device") or ""),
            service=str(item.get("service") or ""),
            rule_id=str(item.get("ruleId") or ""),
            scope="alert",
            summary=str(item.get("summary") or item.get("scenario") or ""),
            payload=dict(item),
        ))

    for item in snapshot.get("suggestions") or []:
        source_id = _suggestion_id(item)
        occurred_at = str(item.get("ts") or "").strip()
        if not source_id or not occurred_at:
            continue
        related: list[SourceReference] = []
        for alert_id in item.get("sourceAlertIds") or []:
            alert_id = str(alert_id).strip()
            if alert_id:
                related.append(SourceReference("alert", alert_id))
        incident_ref = str(item.get("incidentRef") or "").strip()
        if incident_ref:
            related.append(SourceReference("incident", incident_ref))
        repository.ingest(CaseObservation(
            source=SourceReference("suggestion", source_id),
            occurred_at=occurred_at,
            severity=str(item.get("severity") or ""),
            subject=str(item.get("deviceKey") or item.get("device") or ""),
            service=str(item.get("service") or ""),
            rule_id=str(item.get("ruleId") or ""),
            scope=str(item.get("scope") or "suggestion"),
            summary=str(item.get("summary") or ""),
            related_sources=tuple(related),
            hypotheses=dict(item.get("hypothesisSet") or {}),
            timeline=tuple(item.get("timeline") or ()),
            payload=dict(item),
        ))

    for item in snapshot.get("suggestions") or []:
        source_id = _suggestion_id(item)
        if source_id:
            item["caseId"] = repository.case_id_for(
                SourceReference("suggestion", source_id)
            )
    for item in snapshot.get("feed") or []:
        if item.get("kind") == "alert":
            source_id = _alert_id(item)
            kind = "alert"
        else:
            source_id = str(item.get("id") or "").removeprefix("feed-suggestion-")
            kind = "suggestion"
        if source_id:
            item["caseId"] = repository.case_id_for(SourceReference(kind, source_id))
    return snapshot
