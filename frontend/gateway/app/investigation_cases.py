"""Join landed live-situation records to durable investigation cases."""
from __future__ import annotations

from typing import Any

from domains.network_rca.investigation_case import (
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)


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
