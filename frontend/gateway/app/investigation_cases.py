"""Join landed live-situation records to durable investigation cases."""
from __future__ import annotations

import os
import re
import hashlib
import ipaddress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from domains.network_rca.investigation_case import (
    CaseEvent,
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


def _case_family(rule_id: str, service: str, summary: str) -> str | None:
    text = " ".join((rule_id, service, summary)).casefold()
    if any(marker in text for marker in ("bruteforce", "login", "lockout", "admin_auth")):
        return "fam-management-auth"
    if any(marker in text for marker in ("deny", "policy", "firewall", "拒绝", "策略")):
        return "fam-policy-reachability"
    if any(marker in text for marker in (
        "arp", "dhcp", "address", "mac", "identity", "ownership", "duplicate_ip", "地址",
    )):
        return "fam-address-ownership"
    if any(marker in text for marker in ("service", "health", "cpu", "memory", "服务", "内存")):
        return "fam-perception-selfheal"
    if any(marker in text for marker in ("exposure", "port", "listen", "暴露", "端口")):
        return "fam-exposure"
    return None


def _bounded_environment_facts(finding: dict[str, Any], checked_at: str) -> dict[str, Any]:
    """Keep the current measurements needed by a probe, without copying a whole sweep."""
    measured = dict(finding.get("measured") or {})
    transitions = list(measured.get("transitions") or ())
    if len(transitions) > 24:
        measured["transitions"] = [*transitions[:4], *transitions[-20:]]
        measured["transitions_truncated"] = max(
            int(measured.get("transitions_truncated") or 0), len(transitions) - 24,
        )
    verification = dict(finding.get("verification") or {})
    return {
        "dataClassification": "observed",
        "observedAt": checked_at,
        "environmentFindingId": str(finding.get("finding_id") or ""),
        "detector": str(finding.get("detector") or ""),
        "faultClass": str(finding.get("fault_class") or ""),
        "segment": str(finding.get("segment") or ""),
        "subjectKind": str(finding.get("subject_kind") or ""),
        "confidence": finding.get("confidence"),
        "measured": measured,
        "verification": verification,
        "cannotProve": list(finding.get("cannot_prove") or ()),
        "nextProbe": str(finding.get("next_probe") or ""),
        "evidenceSource": str(dict(finding.get("evidence") or {}).get("source") or ""),
    }


def sync_environment_cases(
    report: dict[str, Any], repository: InvestigationCaseRepository,
) -> list[str]:
    """Promote fresh, confirmed environment contradictions into durable cases.

    Medium inventory gaps stay in the environment report.  Automatic investigation
    is reserved for a high-impact condition that a current source has confirmed,
    which prevents the address inventory from becoming a second alert flood.
    """
    checked_at = str(report.get("checked_at") or "").strip()
    if not checked_at:
        return []
    case_ids: list[str] = []
    for finding in report.get("findings") or ():
        if not isinstance(finding, dict):
            continue
        verification = dict(finding.get("verification") or {})
        severity = str(finding.get("severity") or "").casefold()
        source_id = str(finding.get("finding_id") or "").strip()
        subject = str(finding.get("subject") or "").strip()
        if (
            verification.get("state") != "confirmed"
            or severity not in {"high", "critical"}
            or not source_id
            or not subject
        ):
            continue
        facts = _bounded_environment_facts(finding, checked_at)
        headline = str(finding.get("headline") or source_id)
        case = repository.ingest(CaseObservation(
            source=SourceReference("environment_finding", source_id),
            occurred_at=checked_at,
            severity=severity,
            subject=subject,
            service=str(finding.get("fault_class") or "environment"),
            rule_id=str(finding.get("detector") or "environment_sweep"),
            scope=str(finding.get("segment") or "environment"),
            summary=headline,
            payload={
                "dataClassification": "observed",
                "headline": headline,
                "incidentFacts": facts,
            },
        ))
        case_ids.append(case.case_id)
    return case_ids


def derive_investigation_scope(case: Any) -> dict[str, Any]:
    """Translate one durable case into the exact scope used by retrieval and probes."""
    from domains.network_rca.incident_scope import derive_incident_scope

    payload_text = str(case.source_payload)
    identifiers = [
        *_IP.findall(" ".join((case.summary, payload_text))),
        *_MAC.findall(" ".join((case.summary, payload_text))),
    ]
    family = _case_family(case.rule_id, case.service, case.summary)
    try:
        from .rca_reader import _load_topology

        topology = _load_topology() or {}
    except Exception:
        topology = {}
    incident_facts = dict(case.source_payload.get("incidentFacts") or {})
    bounded = derive_incident_scope(
        subject=case.subject or None,
        service=case.service or None,
        first_seen_at=case.first_seen_at,
        last_seen_at=case.last_seen_at,
        facts=incident_facts,
        managed_gateway=_managed_gateway(),
        fault_family=family,
        topology=topology,
        textual_identifiers=identifiers,
    )
    question = case.summary.strip() or case.title.strip() or "调查这起网络事件的根因"
    return {
        "question": question,
        "family": family,
        **bounded.as_dict(),
        "source_refs": [
            {"kind": ref.kind, "source_id": ref.source_id}
            for ref in case.sources
        ],
        "incident_facts": incident_facts,
    }


def recurrence_signature(case: Any) -> str:
    """Stable cohort key for separate incidents with the same investigation scope."""
    scope = derive_investigation_scope(case)
    identity = "\0".join((
        str(scope.get("family") or "unknown"),
        str(scope.get("fault_domain") or "unresolved"),
        str(case.service or ""),
        *sorted(str(value) for value in scope.get("asset_ids") or ()),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _prior_recurrence(
    repository: InvestigationCaseRepository,
    current: Any,
) -> Any | None:
    signature = recurrence_signature(current)
    return next(
        (
            candidate
            for candidate in repository.list(limit=500)
            if candidate.case_id != current.case_id
            and candidate.status in {"resolved", "escalated"}
            and recurrence_signature(candidate) == signature
            and candidate.latest_event("investigation_session_started") is not None
        ),
        None,
    )


def _is_routine_lan_broadcast(facts: dict[str, Any]) -> bool:
    source = str(facts.get("sourceIp") or "")
    destination = str(facts.get("destinationIp") or "")
    try:
        source_ip = ipaddress.ip_address(source)
        destination_ip = ipaddress.ip_address(destination)
    except ValueError:
        return False
    return bool(
        source_ip.is_private
        and str(facts.get("sourceInterfaceRole") or "").casefold() == "lan"
        and str(facts.get("trafficSubtype") or "").casefold() == "local"
        and str(facts.get("action") or "").casefold() == "deny"
        and (destination_ip.is_multicast or destination.endswith(".255"))
    )


def resolve_routine_observations(
    repository: InvestigationCaseRepository, *, limit: int = 500,
) -> int:
    """Close already-landed LAN group traffic without creating Agent sessions."""
    transitions: list[CaseEvent] = []
    for case in repository.list(status="open", limit=limit):
        facts = dict(case.source_payload.get("incidentFacts") or {})
        if not _is_routine_lan_broadcast(facts):
            continue
        decision = {
            "caseId": case.case_id,
            "sessionId": "",
            "state": "resolved",
            "classification": "routine_observation_suppressed",
            "headline": "内网组播或广播拒绝记录保留为观测，不启动事故调查",
            "summary": (
                f"{facts.get('sourceIp')} 发往 {facts.get('destinationIp')} 的 "
                f"{facts.get('service') or '组流量'} 已由本机策略拒绝。"
            ),
            "disposition": "保留源记录用于趋势统计；只有目标变为单播资产或影响业务时重新开案。",
            "action": "不执行操作",
            "impactedAssets": [],
            "evidence": [],
            "missingObservations": [],
            "nextProbe": None,
            "readback": None,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }
        transitions.append(CaseEvent(
            case_id=case.case_id,
            kind="business_decision_recorded",
            payload={"decision": decision},
            status="resolved",
            event_id=f"{case.case_id}:routine-observation-suppressed",
        ))
    return repository.append_events(transitions)


def auto_start_pending_cases(
    repository: InvestigationCaseRepository,
    *,
    limit: int = 4,
    case_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Drive each fresh case through scope, investigation, action and readback."""
    from .investigate import analyze, complete, remediate, start

    started: list[dict[str, Any]] = []
    from domains.network_rca.business_decision import is_terminal_local_in_deny

    max_age = max(30.0, float(os.getenv(
        "AUTOPOIESIS_AUTO_INVESTIGATE_MAX_AGE_SECONDS", "300"
    )))
    backfill_age = max(max_age, float(os.getenv(
        "AUTOPOIESIS_DECISION_BACKFILL_MAX_AGE_SECONDS", "86400"
    )))
    now = datetime.now(timezone.utc)
    candidates = [
        case for case in repository.list(limit=max(1, limit * 6))
        if case.status in {"open", "investigating"}
        and (case_ids is None or case.case_id in case_ids)
        # Controlled fault cases have their own single consumer.  Letting the
        # deployed background poller race the acceptance driver can recover a
        # service between paired reads and turn the comparison into two
        # different system states.  The driver supplies exact case ids.
        and (
            case_ids is not None
            or not case.source_payload.get("acceptanceRunId")
        )
    ]
    for case in candidates:
        if str(case.source_payload.get("dataClassification") or "observed") != "observed":
            continue
        try:
            age_seconds = (now - _at(case.last_seen_at)).total_seconds()
        except ValueError:
            continue
        scope = derive_investigation_scope(case)
        deterministic_backfill = is_terminal_local_in_deny(
            dict(scope.get("incident_facts") or {})
        )
        high_impact_backfill = bool(
            scope.get("family") in {"fam-management-auth", "fam-address-ownership"}
            and case.severity.casefold() in {"critical", "error"}
            and scope.get("incident_facts")
        )
        latest_decision_event = case.latest_event("business_decision_recorded") or {}
        latest_classification = str(
            dict(latest_decision_event.get("decision") or {}).get("classification") or ""
        )
        scope_became_actionable = bool(
            latest_classification == "incident_scope_unresolved"
            and scope.get("scope_quality") != "unresolved"
        )
        if (
            case.status == "investigating"
            and not deterministic_backfill
            and not scope_became_actionable
        ):
            continue
        incomplete_policy_backfill = bool(
            scope.get("family") == "fam-policy-reachability"
            and case.latest_suggestion_id
            and not scope.get("incident_facts")
        )
        allowed_age = (
            backfill_age
            if deterministic_backfill or incomplete_policy_backfill or high_impact_backfill
            else max_age
        )
        if age_seconds < -60 or age_seconds > allowed_age:
            continue
        actionable = deterministic_backfill or bool(case.latest_suggestion_id) or case.severity.casefold() in {
            "high", "critical", "error",
        }
        if not actionable:
            continue
        opened = start(
            scope["question"],
            scope["family"],
            scope["subject"],
            case.case_id,
            auto_started=True,
        )
        finished = complete(str(opened["session_id"]))
        decision = dict(finished.get("decision") or {})
        if (
            decision.get("classification") == "open_root_required"
            and os.getenv("AUTOPOIESIS_AUTO_OPEN_INVESTIGATION", "1") != "0"
        ):
            finished = analyze(str(opened["session_id"]))
            decision = dict(finished.get("decision") or decision)
        candidate = dict(finished.get("action_candidate") or {})
        current_case = repository.get(case.case_id)
        prior_case = _prior_recurrence(repository, current_case) if current_case else None
        if (
            prior_case is not None
            and os.getenv("AUTOPOIESIS_AUTO_EVALUATE_RECURRENCE", "1") != "0"
        ):
            try:
                from .investigate import (
                    get as get_session,
                    investigation_metrics,
                    paired_evaluate_case,
                )

                report = paired_evaluate_case(case.case_id)
                prior_event = prior_case.latest_event("investigation_session_started") or {}
                prior_session_id = str(prior_event.get("sessionId") or "")
                prior_metrics = (
                    investigation_metrics(get_session(prior_session_id))
                    if prior_session_id else None
                )
                current_metrics = investigation_metrics(get_session(str(opened["session_id"])))
                observed_same_root = bool(
                    prior_metrics
                    and prior_metrics.get("confirmed_roots")
                    == current_metrics.get("confirmed_roots")
                )
                prior_steps = (prior_metrics or {}).get("steps_to_first_confirmation")
                current_steps = current_metrics.get("steps_to_first_confirmation")
                observed_earlier = bool(
                    isinstance(prior_steps, int)
                    and isinstance(current_steps, int)
                    and current_steps < prior_steps
                )
                prior_probe_count = (prior_metrics or {}).get("probe_count")
                current_probe_count = current_metrics.get("probe_count")
                observed_fewer_probes = bool(
                    isinstance(prior_probe_count, int)
                    and isinstance(current_probe_count, int)
                    and current_probe_count < prior_probe_count
                )
                report["recurrence"] = {
                    "signature": recurrence_signature(case),
                    "prior_case_id": prior_case.case_id,
                    "current_case_id": case.case_id,
                    "prior": prior_metrics,
                    "current": current_metrics,
                    "same_confirmed_root": observed_same_root,
                    "fewer_probes_than_first_incident": observed_fewer_probes,
                    "earlier_confirmation_than_first_incident": observed_earlier,
                    "probe_delta": (
                        prior_probe_count - current_probe_count
                        if isinstance(prior_probe_count, int)
                        and isinstance(current_probe_count, int)
                        else None
                    ),
                    "confirmation_step_delta": (
                        prior_steps - current_steps
                        if isinstance(prior_steps, int) and isinstance(current_steps, int)
                        else None
                    ),
                }
                report["recurrence_value_proven"] = bool(
                    report.get("acceptance", {}).get("business_value_proven")
                    and observed_same_root
                    and observed_fewer_probes
                    and observed_earlier
                )
                repository.append_event(
                    case.case_id,
                    kind="memory_value_measured",
                    payload={"report": report},
                    event_id=f"{opened['session_id']}:memory-value",
                )
                finished["memory_evaluation"] = report
            except (KeyError, ValueError) as error:
                report = {
                    "evaluation_id": f"pair-ineligible-{opened['session_id']}",
                    "case_id": case.case_id,
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "eligible": False,
                    "reason": str(error),
                    "recurrence": {
                        "signature": recurrence_signature(case),
                        "prior_case_id": prior_case.case_id,
                        "current_case_id": case.case_id,
                    },
                    "recurrence_value_proven": False,
                }
                repository.append_event(
                    case.case_id,
                    kind="memory_value_measured",
                    payload={"report": report},
                    event_id=f"{opened['session_id']}:memory-value-ineligible",
                )
                finished["memory_evaluation"] = report
        if (
            decision.get("state") == "action_ready"
            and candidate.get("auto_execute_allowed") is True
            and os.getenv("AUTOPOIESIS_AUTO_REMEDIATE", "1") != "0"
        ):
            finished = remediate(str(opened["session_id"]))
        started.append({**opened, **finished})
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


def _observed(item: dict[str, Any]) -> bool:
    return str(item.get("dataClassification") or "observed") == "observed"


def _merge_incident_facts(
    suggestion: dict[str, Any],
    alert_facts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for alert_id in suggestion.get("sourceAlertIds") or ():
        source = alert_facts.get(str(alert_id)) or {}
        merged.update({key: value for key, value in source.items() if value not in (None, "")})
    projected = dict(suggestion.get("incidentFacts") or {})
    merged.update({key: value for key, value in projected.items() if value not in (None, "")})
    return merged


def _detection_summary(item: dict[str, Any]) -> str:
    facts = dict(item.get("incidentFacts") or {})
    if not facts or not any(
        facts.get(field) not in (None, "")
        for field in (
            "destinationIp", "destinationPort", "policyId", "policyType",
            "denyCount", "trafficSubtype",
        )
    ):
        return str(item.get("summary") or item.get("scenario") or "")
    source = str(facts.get("sourceIp") or item.get("deviceKey") or "未知来源")
    destination = str(facts.get("destinationIp") or "未知目标")
    service = str(facts.get("service") or item.get("service") or "未知服务")
    action = str(facts.get("action") or "未知动作")
    count = facts.get("denyCount")
    window = facts.get("windowSeconds")
    volume = f"{count} 次/{window} 秒" if count is not None and window is not None else "规则命中"
    return f"{source} -> {destination} · {service} · {action} · {volume}"


def sync_snapshot_cases(
    snapshot: dict[str, Any], repository: InvestigationCaseRepository
) -> dict[str, Any]:
    """Persist one poll's records and annotate the response with stable case ids.

    Alerts are ingested first.  A correlated suggestion then names its sample alert
    ids and transactionally merges their provisional cases into one investigation.
    Finally every visible row resolves through the source index, so the UI never has
    to reproduce the grouping rule.
    """
    repository.remove_legacy_reasoning_projection()
    alert_facts: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("feed") or []:
        if item.get("kind") != "alert":
            continue
        if not _observed(item):
            continue
        source_id = _alert_id(item)
        occurred_at = str(item.get("ts") or "").strip()
        if not source_id or not occurred_at:
            continue
        alert_facts[source_id] = dict(item.get("incidentFacts") or {})
        case = repository.ingest(CaseObservation(
            source=SourceReference("alert", source_id),
            occurred_at=occurred_at,
            severity=str(item.get("severity") or ""),
            subject=str(item.get("deviceKey") or item.get("device") or ""),
            service=str(item.get("service") or ""),
            rule_id=str(item.get("ruleId") or ""),
            scope="alert",
            summary=_detection_summary(item),
            payload=dict(item),
        ))

    for item in snapshot.get("suggestions") or []:
        if not _observed(item):
            continue
        source_id = _suggestion_id(item)
        occurred_at = str(item.get("ts") or "").strip()
        if not source_id or not occurred_at:
            continue
        item["incidentFacts"] = _merge_incident_facts(item, alert_facts)
        item["summary"] = _detection_summary(item)
        related: list[SourceReference] = []
        for alert_id in item.get("sourceAlertIds") or []:
            alert_id = str(alert_id).strip()
            if alert_id:
                related.append(SourceReference("alert", alert_id))
        incident_ref = str(item.get("incidentRef") or "").strip()
        if incident_ref:
            related.append(SourceReference("incident", incident_ref))
        case = repository.ingest(CaseObservation(
            source=SourceReference("suggestion", source_id),
            occurred_at=occurred_at,
            severity=str(item.get("severity") or ""),
            subject=str(item.get("deviceKey") or item.get("device") or ""),
            service=str(item.get("service") or ""),
            rule_id=str(item.get("ruleId") or ""),
            scope=str(item.get("scope") or "suggestion"),
            summary=_detection_summary(item),
            related_sources=tuple(related),
            hypotheses={},
            timeline=(),
            payload=dict(item),
        ))
        for transition in item.get("businessTransitions") or ():
            if not isinstance(transition, dict):
                continue
            transition_payload = {
                key: value for key, value in transition.items()
                if key not in {"kind", "ts", "eventId", "caseStatus"}
            }
            if isinstance(transition_payload.get("decision"), dict):
                transition_payload["decision"] = {
                    **transition_payload["decision"],
                    "caseId": case.case_id,
                }
            repository.append_event(
                case.case_id,
                kind=str(transition.get("kind") or "source_transition"),
                payload=transition_payload,
                status=str(transition.get("caseStatus") or "investigating"),
                occurred_at=str(transition.get("ts") or occurred_at),
                event_id=str(transition.get("eventId") or "") or None,
            )

    # Alert source rows may carry exact fields that were absent from an older
    # suggestion projection. Rebuild the case projection after both inputs land.
    repository.remove_legacy_reasoning_projection()

    for item in snapshot.get("suggestions") or []:
        if not _observed(item):
            continue
        source_id = _suggestion_id(item)
        if source_id:
            item["caseId"] = repository.case_id_for(
                SourceReference("suggestion", source_id)
            )
            if item["caseId"]:
                case = repository.get(str(item["caseId"]))
                item["caseDecision"] = (
                    case.as_dict().get("businessDecision") if case is not None else None
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
            if item["caseId"]:
                case = repository.get(str(item["caseId"]))
                item["caseDecision"] = (
                    case.as_dict().get("businessDecision") if case is not None else None
                )
    existing_case_ids = {
        str(item.get("caseId") or "")
        for item in snapshot.get("suggestions") or ()
        if item.get("caseId")
    }
    projected: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc).timestamp() - 48 * 3600
    for case in repository.list(limit=100):
        if case.case_id in existing_case_ids:
            continue
        if str(case.source_payload.get("dataClassification") or "observed") != "observed":
            continue
        try:
            if _at(case.last_seen_at).timestamp() < cutoff:
                continue
        except ValueError:
            continue
        decision = case.as_dict().get("businessDecision")
        classification = str(dict(decision or {}).get("classification") or "")
        if classification == "routine_observation_suppressed":
            continue
        if case.severity.casefold() not in {"high", "critical", "error"} and not decision:
            continue
        priority = "P1" if case.severity.casefold() in {"critical", "error"} else "P2"
        projected.append({
            "id": f"durable-{case.case_id}",
            "ts": case.last_seen_at,
            "scope": case.scope or "investigation-case",
            "severity": case.severity,
            "priority": priority,
            "summary": case.summary,
            "caseId": case.case_id,
            "service": case.service,
            "device": case.subject,
            "deviceKey": case.subject,
            "clusterSize": case.occurrence_count,
            "incidentFacts": dict(case.source_payload.get("incidentFacts") or {}),
            "caseDecision": decision,
            "dataClassification": "observed",
        })
    if projected:
        suggestions = [*(snapshot.get("suggestions") or []), *projected]
        suggestions.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
        snapshot["suggestions"] = suggestions[:20]
        snapshot["ready"] = True
        snapshot["defaultSuggestionId"] = str(snapshot["suggestions"][0].get("id") or "")
        runtime = dict(snapshot.get("runtime") or {})
        runtime["latestSuggestionTs"] = str(snapshot["suggestions"][0].get("ts") or "")
        snapshot["runtime"] = runtime
    return snapshot
