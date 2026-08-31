"""Durable business record for a live network investigation.

The real-time pipeline can describe the same incident more than once: first as
individual alerts and later as a correlated suggestion that names those alerts.
This repository turns those deliveries into one stable case.  Source references
have a database uniqueness constraint, so polling the landed files repeatedly is
idempotent and a later cluster can merge alert-only cases without inventing a new
operator-visible incident.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


_LEGACY_PRESENTATION_FIELDS = {
    "adaptiveMode",
    "triggerReasons",
    "impactLevel",
    "stageTelemetry",
    "hypothesisSet",
    "runbookDraft",
    "reviewVerdict",
    "timeline",
}


def _fact_summary(facts: dict[str, Any], fallback: str) -> str:
    if not facts:
        return fallback
    flow_fields = {
        "sourceIp", "destinationIp", "service", "action", "denyCount", "windowSeconds",
    }
    if not any(facts.get(field) not in (None, "") for field in flow_fields):
        return fallback
    source = str(facts.get("sourceIp") or "未知来源")
    destination = str(facts.get("destinationIp") or "未知目标")
    service = str(facts.get("service") or "未知服务")
    action = str(facts.get("action") or "未知动作")
    count = facts.get("denyCount")
    window = facts.get("windowSeconds")
    volume = (
        f"{count} 次/{window} 秒"
        if count is not None and window is not None
        else "规则命中"
    )
    return f"{source} -> {destination} · {service} · {action} · {volume}"


def _merge_timeline(existing: Any, incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union source observations with case transitions without losing either."""
    out = [dict(item) for item in existing if isinstance(item, dict)]
    seen = {_json(item) for item in out}
    for item in incoming:
        if not isinstance(item, dict):
            continue
        marker = _json(item)
        if marker not in seen:
            out.append(dict(item))
            seen.add(marker)
    return out


@dataclass(frozen=True)
class SourceReference:
    kind: str
    source_id: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.source_id.strip():
            raise ValueError("source kind and id are required")


@dataclass(frozen=True)
class CaseObservation:
    """One landed alert, suggestion, or controlled-response observation."""

    source: SourceReference
    occurred_at: str
    severity: str = ""
    subject: str = ""
    service: str = ""
    rule_id: str = ""
    scope: str = ""
    summary: str = ""
    related_sources: tuple[SourceReference, ...] = ()
    hypotheses: dict[str, Any] = field(default_factory=dict)
    timeline: tuple[dict[str, Any], ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def all_sources(self) -> tuple[SourceReference, ...]:
        seen: set[tuple[str, str]] = set()
        out: list[SourceReference] = []
        for ref in (self.source, *self.related_sources):
            key = (ref.kind, ref.source_id)
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return tuple(out)


@dataclass(frozen=True)
class CaseEvent:
    """One idempotent case transition suitable for transactional batching."""

    case_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    occurred_at: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class InvestigationCase:
    case_id: str
    status: str
    severity: str
    subject: str
    service: str
    rule_id: str
    scope: str
    title: str
    summary: str
    first_seen_at: str
    last_seen_at: str
    created_at: str
    updated_at: str
    occurrence_count: int
    latest_suggestion_id: str | None
    hypotheses: dict[str, Any]
    timeline: tuple[dict[str, Any], ...]
    source_payload: dict[str, Any]
    sources: tuple[SourceReference, ...]
    version: int

    def latest_event(self, kind: str) -> dict[str, Any] | None:
        return next(
            (dict(item) for item in reversed(self.timeline) if item.get("kind") == kind),
            None,
        )

    def as_dict(self) -> dict[str, Any]:
        decision_event = self.latest_event("business_decision_recorded")
        decision = dict(decision_event.get("decision") or {}) if decision_event else None
        session_event = self.latest_event("investigation_session_started")
        return {
            "caseId": self.case_id,
            "status": self.status,
            "severity": self.severity,
            "subject": self.subject,
            "service": self.service,
            "ruleId": self.rule_id,
            "scope": self.scope,
            "title": self.title,
            "summary": self.summary,
            "firstSeenAt": self.first_seen_at,
            "lastSeenAt": self.last_seen_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "occurrenceCount": self.occurrence_count,
            "latestSuggestionId": self.latest_suggestion_id,
            "hypotheses": self.hypotheses,
            "timeline": list(self.timeline),
            "businessDecision": decision,
            "investigationSessionId": (
                session_event.get("sessionId") if session_event is not None else None
            ),
            "sourcePayload": self.source_payload,
            "sources": [
                {"kind": ref.kind, "sourceId": ref.source_id}
                for ref in self.sources
            ],
            "version": self.version,
        }


class InvestigationCaseRepository:
    """SQLite repository with transactional source deduplication and case merging."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigation_cases (
                    case_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    service TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    latest_suggestion_id TEXT,
                    hypotheses_json TEXT NOT NULL,
                    timeline_json TEXT NOT NULL,
                    source_payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigation_case_sources (
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    case_id TEXT NOT NULL REFERENCES investigation_cases(case_id)
                        ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (source_kind, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_investigation_cases_updated
                    ON investigation_cases(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_investigation_sources_case
                    ON investigation_case_sources(case_id);
                """
            )

    def remove_legacy_reasoning_projection(self) -> int:
        """Remove draft-model artifacts from the operator-facing case projection.

        Immutable source rows retain the delivered records for audit. The case
        projection keeps exact incident facts and completed business events.
        """
        removed = 0
        legacy_kinds = {"inference", "suggestion", "critique", "runbook"}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT case_id, summary, latest_suggestion_id, hypotheses_json, timeline_json, "
                "source_payload_json FROM investigation_cases"
            ).fetchall()
            for row in rows:
                timeline = list(_loads(row["timeline_json"], []))
                deterministic_terminal = any(
                    item.get("kind") == "business_decision_recorded"
                    and dict(item.get("decision") or {}).get("classification")
                    == "blocked_external_probe"
                    for item in timeline
                )
                cleaned = [
                    item for item in timeline
                    if item.get("kind") not in legacy_kinds
                    and not (
                        deterministic_terminal
                        and item.get("kind") == "retrieval_completed"
                    )
                ]
                hypotheses = dict(_loads(row["hypotheses_json"], {}))
                payload = dict(_loads(row["source_payload_json"], {}))
                deleted_fields = [
                    key for key in _LEGACY_PRESENTATION_FIELDS if key in payload
                ]
                for key in deleted_fields:
                    payload.pop(key, None)
                incident_facts = dict(payload.get("incidentFacts") or {})
                if not incident_facts:
                    source_rows = conn.execute(
                        "SELECT payload_json FROM investigation_case_sources "
                        "WHERE case_id=? ORDER BY occurred_at DESC",
                        (row["case_id"],),
                    ).fetchall()
                    for source_row in source_rows:
                        source_payload = dict(_loads(source_row["payload_json"], {}))
                        candidate = dict(source_payload.get("incidentFacts") or {})
                        if candidate:
                            incident_facts = candidate
                            payload["incidentFacts"] = candidate
                            payload["dataClassification"] = str(
                                candidate.get("dataClassification")
                                or payload.get("dataClassification")
                                or "observed"
                            )
                            break
                summary = (
                    _fact_summary(incident_facts, str(row["summary"]))
                    if incident_facts
                    else (
                        "历史检测记录缺少结构化源字段，案件需要从事实库补查。"
                        if deleted_fields or row["latest_suggestion_id"]
                        else str(row["summary"])
                    )
                )
                payload_changed = payload != dict(_loads(row["source_payload_json"], {}))
                if (
                    cleaned == timeline
                    and not hypotheses
                    and not payload_changed
                    and summary == row["summary"]
                ):
                    continue
                removed += (
                    len(timeline) - len(cleaned)
                    + (1 if hypotheses else 0)
                    + len(deleted_fields)
                )
                conn.execute(
                    "UPDATE investigation_cases SET summary=?, hypotheses_json='{}', "
                    "timeline_json=?, source_payload_json=?, updated_at=?, "
                    "version=version+1 WHERE case_id=?",
                    (
                        summary,
                        _json(cleaned),
                        _json(payload),
                        _utc_now(),
                        row["case_id"],
                    ),
                )
            conn.commit()
        return removed

    @staticmethod
    def _new_case_id(source: SourceReference) -> str:
        digest = hashlib.sha256(
            f"{source.kind}\0{source.source_id}".encode("utf-8")
        ).hexdigest()[:20]
        return f"case-{digest}"

    @staticmethod
    def _title(observation: CaseObservation) -> str:
        topic = observation.rule_id or observation.service or observation.scope or "live-event"
        return f"{topic} · {observation.subject}" if observation.subject else topic

    def ingest(self, observation: CaseObservation) -> InvestigationCase:
        """Create, update, or merge the case named by this observation's sources."""
        refs = observation.all_sources
        if not refs:
            raise ValueError("an observation requires at least one source")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_ids = self._case_ids_for(conn, refs)
            if existing_ids:
                canonical = self._select_canonical(conn, existing_ids)
                self._merge_cases(conn, canonical, existing_ids - {canonical})
            else:
                canonical = self._new_case_id(observation.source)
                conn.execute(
                    """
                    INSERT INTO investigation_cases (
                        case_id, status, severity, subject, service, rule_id, scope,
                        title, summary, first_seen_at, last_seen_at, created_at,
                        updated_at, occurrence_count, latest_suggestion_id,
                        hypotheses_json, timeline_json, source_payload_json, version
                    ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1)
                    """,
                    (
                        canonical,
                        observation.severity,
                        observation.subject,
                        observation.service,
                        observation.rule_id,
                        observation.scope,
                        self._title(observation),
                        observation.summary,
                        observation.occurred_at,
                        observation.occurred_at,
                        now,
                        now,
                        observation.source.source_id
                        if observation.source.kind == "suggestion" else None,
                        _json(observation.hypotheses),
                        _json(list(observation.timeline)),
                        _json(observation.payload),
                    ),
                )

            before = conn.execute(
                "SELECT * FROM investigation_cases WHERE case_id=?", (canonical,)
            ).fetchone()
            assert before is not None
            for ref in refs:
                payload = observation.payload if ref == observation.source else {}
                conn.execute(
                    """
                    INSERT INTO investigation_case_sources (
                        source_kind, source_id, case_id, occurred_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_kind, source_id) DO UPDATE SET
                        case_id=excluded.case_id,
                        occurred_at=CASE
                            WHEN excluded.occurred_at > occurred_at THEN excluded.occurred_at
                            ELSE occurred_at END,
                        payload_json=CASE
                            WHEN excluded.payload_json != '{}' THEN excluded.payload_json
                            ELSE payload_json END
                    """,
                    (ref.kind, ref.source_id, canonical, observation.occurred_at, _json(payload)),
                )

            source_count = self._occurrence_count(conn, canonical)
            latest_suggestion = (
                observation.source.source_id
                if observation.source.kind == "suggestion"
                else before["latest_suggestion_id"]
            )
            use_detail = observation.source.kind == "suggestion" or not before["latest_suggestion_id"]
            candidate = {
                "severity": observation.severity or before["severity"],
                "subject": observation.subject or before["subject"],
                "service": observation.service or before["service"],
                "rule_id": observation.rule_id or before["rule_id"],
                "scope": observation.scope or before["scope"],
                "title": self._title(observation) if use_detail else before["title"],
                "summary": observation.summary if use_detail and observation.summary else before["summary"],
                "first_seen_at": min(before["first_seen_at"], observation.occurred_at),
                "last_seen_at": max(before["last_seen_at"], observation.occurred_at),
                "occurrence_count": source_count,
                "latest_suggestion_id": latest_suggestion,
                "hypotheses_json": (
                    _json(observation.hypotheses) if use_detail and observation.hypotheses
                    else before["hypotheses_json"]
                ),
                "timeline_json": _json(_merge_timeline(
                    _loads(before["timeline_json"], []),
                    observation.timeline if use_detail else (),
                )),
                "source_payload_json": (
                    _json(observation.payload) if use_detail and observation.payload
                    else before["source_payload_json"]
                ),
            }
            changed = any(candidate[key] != before[key] for key in candidate)
            if changed:
                conn.execute(
                    """
                    UPDATE investigation_cases SET
                        severity=?, subject=?, service=?, rule_id=?, scope=?, title=?,
                        summary=?, first_seen_at=?, last_seen_at=?, updated_at=?,
                        occurrence_count=?, latest_suggestion_id=?, hypotheses_json=?,
                        timeline_json=?, source_payload_json=?, version=version+1
                    WHERE case_id=?
                    """,
                    (
                        candidate["severity"], candidate["subject"], candidate["service"],
                        candidate["rule_id"], candidate["scope"], candidate["title"],
                        candidate["summary"], candidate["first_seen_at"], candidate["last_seen_at"],
                        now, candidate["occurrence_count"], candidate["latest_suggestion_id"],
                        candidate["hypotheses_json"], candidate["timeline_json"],
                        candidate["source_payload_json"], canonical,
                    ),
                )
            conn.commit()
        case = self.get(canonical)
        assert case is not None
        return case

    def _case_ids_for(
        self, conn: sqlite3.Connection, refs: Iterable[SourceReference]
    ) -> set[str]:
        out: set[str] = set()
        for ref in refs:
            row = conn.execute(
                "SELECT case_id FROM investigation_case_sources WHERE source_kind=? AND source_id=?",
                (ref.kind, ref.source_id),
            ).fetchone()
            if row:
                out.add(str(row["case_id"]))
        return out

    @staticmethod
    def _select_canonical(conn: sqlite3.Connection, case_ids: set[str]) -> str:
        marks = ",".join("?" for _ in case_ids)
        row = conn.execute(
            f"SELECT case_id FROM investigation_cases WHERE case_id IN ({marks}) "
            "ORDER BY first_seen_at ASC, created_at ASC, case_id ASC LIMIT 1",
            tuple(case_ids),
        ).fetchone()
        if row is None:
            raise RuntimeError("source index references a missing investigation case")
        return str(row["case_id"])

    @staticmethod
    def _merge_cases(conn: sqlite3.Connection, canonical: str, duplicates: set[str]) -> None:
        if not duplicates:
            return
        canonical_row = conn.execute(
            "SELECT * FROM investigation_cases WHERE case_id=?", (canonical,)
        ).fetchone()
        if canonical_row is None:
            raise RuntimeError("canonical investigation case is missing")
        first_seen = str(canonical_row["first_seen_at"])
        last_seen = str(canonical_row["last_seen_at"])
        created_at = str(canonical_row["created_at"])
        timeline = list(_loads(canonical_row["timeline_json"], []))
        status = str(canonical_row["status"])
        for duplicate in duplicates:
            row = conn.execute(
                "SELECT * FROM investigation_cases WHERE case_id=?", (duplicate,)
            ).fetchone()
            if row is None:
                continue
            first_seen = min(first_seen, str(row["first_seen_at"]))
            last_seen = max(last_seen, str(row["last_seen_at"]))
            created_at = min(created_at, str(row["created_at"]))
            timeline = _merge_timeline(timeline, _loads(row["timeline_json"], []))
            if status == "open" and row["status"] != "open":
                status = str(row["status"])
            conn.execute(
                "UPDATE investigation_case_sources SET case_id=? WHERE case_id=?",
                (canonical, duplicate),
            )
            conn.execute("DELETE FROM investigation_cases WHERE case_id=?", (duplicate,))
        conn.execute(
            "UPDATE investigation_cases SET status=?, first_seen_at=?, last_seen_at=?, "
            "created_at=?, timeline_json=?, version=version+1 WHERE case_id=?",
            (status, first_seen, last_seen, created_at, _json(timeline), canonical),
        )

    @staticmethod
    def _occurrence_count(conn: sqlite3.Connection, case_id: str) -> int:
        alert_count = int(conn.execute(
            "SELECT COUNT(*) FROM investigation_case_sources WHERE case_id=? AND source_kind='alert'",
            (case_id,),
        ).fetchone()[0])
        if alert_count:
            return alert_count
        return int(conn.execute(
            "SELECT COUNT(*) FROM investigation_case_sources WHERE case_id=?",
            (case_id,),
        ).fetchone()[0])

    def case_id_for(self, source: SourceReference) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT case_id FROM investigation_case_sources WHERE source_kind=? AND source_id=?",
                (source.kind, source.source_id),
            ).fetchone()
        return str(row["case_id"]) if row else None

    def get(self, case_id: str) -> InvestigationCase | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM investigation_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if row is None:
                return None
            sources = conn.execute(
                "SELECT source_kind, source_id FROM investigation_case_sources "
                "WHERE case_id=? ORDER BY occurred_at ASC, source_kind ASC, source_id ASC",
                (case_id,),
            ).fetchall()
        return self._from_row(row, sources)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[InvestigationCase]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM investigation_cases WHERE status=? "
                    "ORDER BY last_seen_at DESC LIMIT ?", (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM investigation_cases ORDER BY last_seen_at DESC LIMIT ?", (limit,)
                ).fetchall()
            cases: list[InvestigationCase] = []
            for row in rows:
                sources = conn.execute(
                    "SELECT source_kind, source_id FROM investigation_case_sources "
                    "WHERE case_id=? ORDER BY occurred_at ASC, source_kind ASC, source_id ASC",
                    (row["case_id"],),
                ).fetchall()
                cases.append(self._from_row(row, sources))
        return cases

    def open(self, case_id: str, *, actor: str = "operator") -> InvestigationCase | None:
        """Persist an idempotent transition from queued to active investigation."""
        case = self.get(case_id)
        if case is None:
            return None
        if case.status != "open":
            return case
        return self.append_event(
            case_id,
            kind="case_opened",
            payload={"actor": actor},
            status="investigating",
        )

    def append_event(
        self,
        case_id: str,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> InvestigationCase | None:
        """Append a durable investigation transition for probes, turns, or decisions.

        ``event_id`` makes callers such as an investigation-session adapter
        idempotent.  The identifier is stored in the timeline itself, keeping this
        API usable before a separate workflow/event table is warranted.
        """
        kind = kind.strip()
        if not kind:
            raise ValueError("event kind is required")
        if status is not None and status not in {
            "open", "investigating", "waiting", "resolved", "escalated", "closed"
        }:
            raise ValueError("unsupported investigation case status")
        now = occurred_at or _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, timeline_json FROM investigation_cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            timeline = list(_loads(row["timeline_json"], []))
            if event_id and any(item.get("eventId") == event_id for item in timeline):
                conn.commit()
                return self.get(case_id)
            event = {"kind": kind, "ts": now, **dict(payload or {})}
            if event_id:
                event["eventId"] = event_id
            timeline.append(event)
            conn.execute(
                "UPDATE investigation_cases SET status=?, updated_at=?, "
                "timeline_json=?, version=version+1 WHERE case_id=?",
                (status or row["status"], now, _json(timeline), case_id),
            )
            conn.commit()
        return self.get(case_id)

    def append_events(self, events: Iterable[CaseEvent]) -> int:
        """Append many independent transitions with one durable commit.

        This is used for bounded migrations and detector projections.  A single
        transaction avoids one filesystem sync per case on local-path storage,
        while per-case event identifiers preserve the same idempotency contract
        as :meth:`append_event`.
        """
        pending = list(events)
        if not pending:
            return 0
        allowed_statuses = {
            "open", "investigating", "waiting", "resolved", "escalated", "closed",
        }
        updated = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in pending:
                kind = item.kind.strip()
                if not item.case_id.strip() or not kind:
                    raise ValueError("case id and event kind are required")
                if item.status is not None and item.status not in allowed_statuses:
                    raise ValueError("unsupported investigation case status")
                row = conn.execute(
                    "SELECT status, timeline_json FROM investigation_cases WHERE case_id=?",
                    (item.case_id,),
                ).fetchone()
                if row is None:
                    continue
                timeline = list(_loads(row["timeline_json"], []))
                if item.event_id and any(
                    event.get("eventId") == item.event_id for event in timeline
                ):
                    continue
                now = item.occurred_at or _utc_now()
                event = {"kind": kind, "ts": now, **dict(item.payload)}
                if item.event_id:
                    event["eventId"] = item.event_id
                timeline.append(event)
                conn.execute(
                    "UPDATE investigation_cases SET status=?, updated_at=?, "
                    "timeline_json=?, version=version+1 WHERE case_id=?",
                    (item.status or row["status"], now, _json(timeline), item.case_id),
                )
                updated += 1
            conn.commit()
        return updated

    @staticmethod
    def _from_row(
        row: sqlite3.Row, sources: Iterable[sqlite3.Row]
    ) -> InvestigationCase:
        return InvestigationCase(
            case_id=str(row["case_id"]),
            status=str(row["status"]),
            severity=str(row["severity"]),
            subject=str(row["subject"]),
            service=str(row["service"]),
            rule_id=str(row["rule_id"]),
            scope=str(row["scope"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            first_seen_at=str(row["first_seen_at"]),
            last_seen_at=str(row["last_seen_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            occurrence_count=int(row["occurrence_count"]),
            latest_suggestion_id=row["latest_suggestion_id"],
            hypotheses=dict(_loads(row["hypotheses_json"], {})),
            timeline=tuple(_loads(row["timeline_json"], [])),
            source_payload=dict(_loads(row["source_payload_json"], {})),
            sources=tuple(
                SourceReference(kind=str(source["source_kind"]), source_id=str(source["source_id"]))
                for source in sources
            ),
            version=int(row["version"]),
        )
