"""Join landed live-situation records to durable investigation cases."""
from __future__ import annotations

import os
import re
import hashlib
import ipaddress
import queue
import threading
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

_CONTROLLED_IDENTITY_PREFIXES = (
    "autopoiesis-acceptance-",
    "bvaccept-",
    "controlled-",
    "managed-host-",
    "replay-",
    "synthetic-",
)

_PRESENTATION_ONLY_FIELDS = {
    "adaptiveMode", "triggerReasons", "impactLevel", "stageTelemetry",
    "hypothesisSet", "runbookDraft", "reviewVerdict", "timeline",
}


def is_controlled_identity(value: Any) -> bool:
    """Return whether an operator-visible identity belongs to a test or replay.

    Older acceptance runs were persisted before the explicit classification field
    was added.  Their generated unit and host names are stable, so identity is the
    durable second line of separation between production cases and test evidence.
    """
    normalized = str(value or "").strip().casefold()
    return normalized.startswith(_CONTROLLED_IDENTITY_PREFIXES)


def is_observed_case(case: Any) -> bool:
    """Production projection predicate shared by APIs and background workers."""
    payload = dict(getattr(case, "source_payload", {}) or {})
    facts = dict(payload.get("incidentFacts") or {})
    classification = str(
        payload.get("dataClassification")
        or facts.get("dataClassification")
        or "observed"
    ).casefold()
    identities = (
        getattr(case, "subject", ""),
        payload.get("device"),
        payload.get("deviceKey"),
        payload.get("acceptanceRunId"),
        facts.get("managedDevice"),
    )
    return classification == "observed" and not any(
        is_controlled_identity(identity) for identity in identities
    )


def _has_observed_classification(case: Any) -> bool:
    payload = dict(getattr(case, "source_payload", {}) or {})
    facts = dict(payload.get("incidentFacts") or {})
    return str(
        payload.get("dataClassification")
        or facts.get("dataClassification")
        or "observed"
    ).casefold() == "observed"


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
        "arp", "dhcp", "mac", "identity", "ownership", "duplicate_ip", "地址归属", "地址冲突",
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
    candidates = [
        *repository.list(status="open", limit=limit),
        *repository.list(status="investigating", limit=limit),
        *repository.list(status="resolved", limit=limit),
    ]
    for case in candidates:
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
        latest_decision = case.latest_event("business_decision_recorded") or {}
        latest_classification = str(
            dict(latest_decision.get("decision") or {}).get("classification") or ""
        )
        transition_id = f"{case.case_id}:routine-observation-suppressed"
        if latest_classification and latest_classification != "routine_observation_suppressed":
            marker = hashlib.sha256(
                str(
                    latest_decision.get("eventId")
                    or latest_decision.get("ts")
                    or case.version
                ).encode("utf-8")
            ).hexdigest()[:12]
            transition_id += f":restore:{marker}"
        transitions.append(CaseEvent(
            case_id=case.case_id,
            kind="business_decision_recorded",
            payload={"decision": decision},
            status="resolved",
            event_id=transition_id,
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
    source_candidates = (
        [case for case_id in case_ids if (case := repository.get(case_id)) is not None]
        if case_ids is not None
        else repository.list(limit=max(1, limit * 6))
    )
    candidates = [
        case for case in source_candidates
        if case.status in {"open", "investigating"}
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
        if not (
            _has_observed_classification(case)
            if case_ids is not None
            else is_observed_case(case)
        ):
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
        resumable_bounded_investigation = latest_classification in {
            "open_root_required",
            "root_cause_unresolved",
            "incident_scope_unresolved",
        }
        latest_session = case.latest_event("investigation_session_started") or {}
        abandoned_auto_session = bool(
            not latest_classification and latest_session.get("autoStarted") is True
        )
        if (
            case.status == "investigating"
            and not deterministic_backfill
            and not scope_became_actionable
            and not resumable_bounded_investigation
            and not abandoned_auto_session
        ):
            continue
        incomplete_policy_backfill = bool(
            scope.get("family") == "fam-policy-reachability"
            and case.latest_suggestion_id
            and not scope.get("incident_facts")
        )
        if resumable_bounded_investigation or abandoned_auto_session:
            # An already-started unattended goal remains owed a terminal result
            # regardless of the alert's age. Age limits govern new takeover,
            # not completion of work the service already accepted.
            allowed_age = float("inf")
        elif deterministic_backfill or incomplete_policy_backfill or high_impact_backfill:
            allowed_age = backfill_age
        else:
            allowed_age = max_age
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
            decision.get("classification") in {
                "open_root_required", "root_cause_unresolved",
            }
            and os.getenv("AUTOPOIESIS_AUTO_OPEN_INVESTIGATION", "1") != "0"
        ):
            finished = analyze(str(opened["session_id"]))
            decision = dict(finished.get("decision") or decision)
        candidate = dict(finished.get("action_candidate") or {})
        current_case = repository.get(case.case_id)
        prior_case = _prior_recurrence(repository, current_case) if current_case else None
        recurrence_ineligible_classifications = {
            "authentication_evidence_conflict",
            "bounded_investigation_exhausted",
            "incident_scope_data_gap",
            "incident_scope_unresolved",
            "open_root_required",
            "root_cause_unresolved",
            "policy_outcome_unresolved",
        }
        if (
            prior_case is not None
            and decision.get("classification") not in recurrence_ineligible_classifications
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
        else:
            try:
                from .change_routes import propose_investigation_change
                proposal = propose_investigation_change(str(opened["session_id"]))
                if proposal:
                    finished['configuration_change_id'] = proposal['id']
                    repository.append_event(case.case_id, kind='configuration_change_proposed',
                        payload={'change_id': proposal['id'], 'session_id': opened['session_id'], 'status': proposal['status']},
                        event_id='configuration-change:' + proposal['id'], status='waiting')
            except Exception as error:
                finished['configuration_change_error'] = type(error).__name__ + ':' + str(error)[:200]
        started.append({**opened, **finished})
        if len(started) >= limit:
            break
    return started


class AutoInvestigationDispatcher:
    """Run bounded case goals on daemon workers without blocking event intake."""

    def __init__(
        self,
        repository: InvestigationCaseRepository,
        *,
        workers: int = 2,
        retry_interval_seconds: float = 30.0,
    ) -> None:
        self.repository = repository
        self.workers = max(1, min(8, int(workers)))
        self.retry_interval_seconds = max(1.0, float(retry_interval_seconds))
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=self.workers * 2)
        self._lock = threading.Lock()
        self._claimed: set[str] = set()
        self._last_attempt: dict[str, float] = {}
        self._submitted = 0
        self._completed_cases = 0
        self._failed_jobs = 0
        self._last_age_filtered = 0
        self._last_error: str | None = None
        self._last_completion_at: str | None = None
        self._closed = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"auto-investigation-{index + 1}",
                daemon=True,
            )
            for index in range(self.workers)
        ]
        for thread in self._threads:
            thread.start()

    def dispatch(self, *, limit: int = 4) -> int:
        """Claim recent unresolved cases and return immediately."""
        if self._closed:
            return 0
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            available_capacity = max(0, self.workers * 2 - len(self._claimed))
        if available_capacity == 0:
            return 0
        limit = min(max(1, int(limit)), available_capacity)
        from domains.network_rca.business_decision import is_terminal_local_in_deny

        max_age = max(30.0, float(os.getenv(
            "AUTOPOIESIS_AUTO_INVESTIGATE_MAX_AGE_SECONDS", "300"
        )))
        backfill_age = max(max_age, float(os.getenv(
            "AUTOPOIESIS_DECISION_BACKFILL_MAX_AGE_SECONDS", "86400"
        )))
        observed_now = datetime.now(timezone.utc)

        unresolved = [
            *self.repository.list(status="investigating", limit=5_000),
            *self.repository.list(status="open", limit=5_000),
        ]
        candidates = [
            case for case in unresolved
            if not case.source_payload.get("acceptanceRunId")
            and is_observed_case(case)
        ]
        eligible: list[Any] = []
        age_filtered = 0
        for case in candidates:
            scope = derive_investigation_scope(case)
            facts = dict(scope.get("incident_facts") or {})
            deterministic = is_terminal_local_in_deny(facts)
            latest = case.latest_event("business_decision_recorded") or {}
            classification = str(
                dict(latest.get("decision") or {}).get("classification") or ""
            )
            latest_session = case.latest_event("investigation_session_started") or {}
            abandoned_auto = bool(
                not classification and latest_session.get("autoStarted") is True
            )
            resumable = classification in {
                "open_root_required",
                "root_cause_unresolved",
                "incident_scope_unresolved",
            }
            high_impact_backfill = bool(
                scope.get("family") in {"fam-management-auth", "fam-address-ownership"}
                and case.severity.casefold() in {"critical", "error"}
                and facts
            )
            incomplete_policy_backfill = bool(
                scope.get("family") == "fam-policy-reachability"
                and case.latest_suggestion_id
                and not facts
            )
            allowed_age = (
                float("inf")
                if resumable or abandoned_auto
                else backfill_age
                if deterministic or incomplete_policy_backfill or high_impact_backfill
                else max_age
            )
            try:
                age_seconds = (observed_now - _at(case.last_seen_at)).total_seconds()
            except ValueError:
                age_filtered += 1
                continue
            if age_seconds < -60 or age_seconds > allowed_age:
                age_filtered += 1
                continue
            if case.status == "investigating" and not (
                deterministic
                or resumable
                or abandoned_auto
            ):
                continue
            if not (
                deterministic
                or case.latest_suggestion_id
                or case.severity.casefold() in {"high", "critical", "error"}
            ):
                continue
            eligible.append(case)
        with self._lock:
            self._last_age_filtered = age_filtered
        severity_rank = {"critical": 0, "error": 1, "high": 2}

        def completion_priority(case: Any) -> int:
            latest = case.latest_event("business_decision_recorded") or {}
            classification = str(
                dict(latest.get("decision") or {}).get("classification") or ""
            )
            if classification == "incident_scope_unresolved":
                return 0
            if not classification:
                return 1
            return 2

        candidates = sorted(
            eligible,
            key=lambda case: (
                0 if case.status == "investigating" else 1,
                completion_priority(case),
                severity_rank.get(case.severity.casefold(), 3),
                -_at(case.last_seen_at).timestamp(),
            ),
        )
        submitted = 0
        for case in candidates:
            if submitted >= limit:
                break
            with self._lock:
                last_attempt = self._last_attempt.get(case.case_id, 0.0)
                if (
                    case.case_id in self._claimed
                    or now - last_attempt < self.retry_interval_seconds
                ):
                    continue
                self._claimed.add(case.case_id)
                self._last_attempt[case.case_id] = now
            try:
                self._queue.put_nowait(case.case_id)
            except queue.Full:
                with self._lock:
                    self._claimed.discard(case.case_id)
                break
            with self._lock:
                self._submitted += 1
            submitted += 1
        return submitted

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "workers": self.workers,
                "inFlight": len(self._claimed),
                "queued": self._queue.qsize(),
                "submitted": self._submitted,
                "completedCases": self._completed_cases,
                "failedJobs": self._failed_jobs,
                "lastAgeFiltered": self._last_age_filtered,
                "lastError": self._last_error,
                "lastCompletionAt": self._last_completion_at,
            }

    def close(self) -> None:
        self._closed = True
        for _thread in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break

    def _worker(self) -> None:
        while True:
            case_id = self._queue.get()
            if case_id is None:
                self._queue.task_done()
                return
            try:
                results = auto_start_pending_cases(
                    self.repository,
                    limit=1,
                    case_ids={case_id},
                )
                with self._lock:
                    self._completed_cases += len(results)
                    self._last_error = None
                    self._last_completion_at = datetime.now(timezone.utc).isoformat()
            except Exception as error:  # one case cannot stop the remaining queue
                with self._lock:
                    self._failed_jobs += 1
                    self._last_error = f"{type(error).__name__}: {error}"[:300]
                    self._last_completion_at = datetime.now(timezone.utc).isoformat()
            finally:
                with self._lock:
                    self._claimed.discard(case_id)
                self._queue.task_done()


def _alert_id(feed_item: dict[str, Any]) -> str:
    explicit = str(feed_item.get("sourceId") or "").strip()
    if explicit:
        return explicit
    value = str(feed_item.get("id") or "")
    return value.removeprefix("feed-alert-").strip()


def _suggestion_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def _observed(item: dict[str, Any]) -> bool:
    facts = dict(item.get("incidentFacts") or {})
    classification = str(
        item.get("dataClassification")
        or facts.get("dataClassification")
        or "observed"
    ).casefold()
    identities = (
        item.get("device"), item.get("deviceKey"), item.get("acceptanceRunId"),
        facts.get("managedDevice"),
    )
    return classification == "observed" and not any(
        is_controlled_identity(identity) for identity in identities
    )


def _case_source_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Keep durable facts and decisions, leaving card-only projections in the response."""
    return {
        key: value for key, value in item.items()
        if key not in _PRESENTATION_ONLY_FIELDS
    }


def _continuing_sentinel_sources(
    item: dict[str, Any], repository: InvestigationCaseRepository,
) -> tuple[SourceReference, ...]:
    """Join legacy per-poll cases for one still-active Sentinel condition.

    Earlier projections treated every safety-gated poll as a new incident. The
    current card has a stable cycle id, but the already-landed rows still need a
    bridge into one canonical case. Only unresolved cases with the same exact
    detector, subject and service qualify, so a recovered condition can open a
    fresh case when it returns.
    """
    if str(item.get("scope") or "") != "sentinel":
        return ()
    subject = str(item.get("deviceKey") or item.get("device") or "")
    service = str(item.get("service") or "")
    rule_id = str(item.get("ruleId") or "")
    refs: list[SourceReference] = []
    for case in repository.list(limit=500):
        if (
            case.status not in {"open", "investigating", "waiting", "escalated"}
            or case.subject != subject
            or case.service != service
            or case.rule_id != rule_id
        ):
            continue
        if case.sources:
            refs.append(case.sources[0])
    return tuple(refs)


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
    if facts.get("failedLogins") is not None:
        device = str(facts.get("managedDevice") or item.get("deviceKey") or "受管网关")
        failures = int(facts.get("failedLogins") or 0)
        distinct = int(facts.get("distinctSources") or 0)
        window = int(facts.get("windowSeconds") or 0)
        return f"{device} · {window} 秒内 {failures} 次认证失败 · {distinct} 个公网来源"
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


def prepare_case_repository(repository: InvestigationCaseRepository) -> dict[str, Any]:
    """Apply bounded projection migrations once before the live sync loop starts."""
    try:
        removed = repository.remove_legacy_reasoning_projection()
    except Exception as error:
        return {
            "status": "failed",
            "removed": 0,
            "error": f"{type(error).__name__}: {error}"[:300],
        }
    return {"status": "completed", "removed": removed, "error": None}


def sync_snapshot_cases(
    snapshot: dict[str, Any], repository: InvestigationCaseRepository
) -> dict[str, Any]:
    """Persist one poll's records and annotate the response with stable case ids.

    Alerts are ingested first.  A correlated suggestion then names its sample alert
    ids and transactionally merges their provisional cases into one investigation.
    Finally every visible row resolves through the source index, so the UI never has
    to reproduce the grouping rule.
    """
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
        alert_ref = SourceReference("alert", source_id)
        # Alert ids name immutable landed events. Once indexed, replaying the
        # same feed row cannot add information and should not acquire a SQLite
        # write lock on every UI poll.
        if repository.case_id_for(alert_ref):
            continue
        case = repository.ingest(CaseObservation(
            source=alert_ref,
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
        related.extend(_continuing_sentinel_sources(item, repository))
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
            payload=_case_source_payload(item),
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

    return annotate_snapshot_cases(snapshot, repository)


def annotate_snapshot_cases(
    snapshot: dict[str, Any], repository: InvestigationCaseRepository,
) -> dict[str, Any]:
    """Attach durable case ids and recent case-only rows without writing.

    The background case synchronizer owns persistence. HTTP readers use this
    projection so a five-second page poll never competes with that writer or
    waits for a filesystem commit.
    """

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
        if not is_observed_case(case):
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
