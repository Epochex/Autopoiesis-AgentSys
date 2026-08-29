"""Production coordinator for incident, risk, and feature memory.

The domain objects remain pure and deterministic.  This module owns the
deployment concerns around them: source reads, durable snapshots, refresh
status, investigation recall, and the audit projection consumed by the UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from threading import Lock, RLock
from typing import Any, Callable, Iterable, Mapping, Protocol

from core.env import autopoiesis_env
from core.memory.operational_repository import (
    InMemoryOperationalRepository,
    OperationalKind,
    PostgresOperationalRepository,
)
from domains.network_rca.network_feature import (
    FeatureStore,
    NetworkFeatureEngine,
)
from domains.network_rca.incident_dossier import (
    ActionReceipt,
    EvidenceReference,
    IncidentDossier,
    InMemoryDossierStore,
    ObservationWindow,
    ReadbackCheck,
    ReadbackResult,
    RemediationAttempt,
    from_risk_pattern,
)
from domains.network_rca.risk_pattern import (
    RiskEvent,
    RiskPatternStore,
    risk_event_from_clickhouse_row,
)


_RISK_STORE_ID = "risk-pattern-store-v1"
_FEATURE_STORE_ID = "network-feature-store-v1"
_INCIDENT_RISK_TYPES = frozenset({
    "credential_attack",
    "admin_login_failed",
    "admin_login_lockout",
    "management_exposure",
})
_INCIDENT_RISK_THRESHOLD = 20
_MAX_AGGREGATE_RESTORE_BYTES = int(
    os.getenv("AUTOPOIESIS_OPERATIONAL_RESTORE_MAX_BYTES", str(8 * 1024 * 1024))
)


class OperationalRepository(Protocol):
    def initialize_schema(self) -> None: ...
    def upsert(
        self, kind: OperationalKind, record_id: str, payload: Mapping[str, Any],
        *, expected_version: int | None = None,
    ) -> Any: ...
    def get(self, kind: OperationalKind, record_id: str) -> Any: ...
    def load(self, kind: OperationalKind) -> list[Any]: ...
    def payload_size(self, kind: OperationalKind, record_id: str) -> int | None: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class OperationalMemoryService:
    """Thread-safe orchestration around the three authoritative memory types."""

    def __init__(
        self,
        repository: OperationalRepository | None = None,
        *,
        query: Callable[[str], list[dict[str, Any]]] | None = None,
        timeline_reader: Callable[[], list[dict[str, Any]]] | None = None,
        environment_reader: Callable[[], dict[str, Any]] | None = None,
        durable: bool | None = None,
    ) -> None:
        self.repository = repository or InMemoryOperationalRepository()
        self.durable = (
            not isinstance(self.repository, InMemoryOperationalRepository)
            if durable is None else durable
        )
        self._query = query
        self._timeline_reader = timeline_reader
        self._environment_reader = environment_reader
        self._lock = RLock()
        self._refresh_lock = Lock()
        self.last_refresh: datetime | None = None
        self.source_status: dict[str, str] = {}
        self.risks = RiskPatternStore(
            max_patterns=512,
            max_dimension_values=512,
            max_evidence_refs=64,
            max_event_ids_per_pattern=2_000,
        )
        self.features = NetworkFeatureEngine()
        self.dossiers = InMemoryDossierStore()
        self._restore()

    def _restore(self) -> None:
        restored_dossiers: list[IncidentDossier] = []
        for snapshot in self.repository.load("incident_dossier"):
            if snapshot.payload.get("schema_version") == "1.0":
                dossier = IncidentDossier.model_validate(snapshot.payload)
                self.dossiers.ingest(dossier)
                restored_dossiers.append(dossier)
        payload_size = getattr(self.repository, "payload_size", None)

        def within_restore_budget(kind: OperationalKind, record_id: str) -> bool:
            if not callable(payload_size):
                return True
            size = payload_size(kind, record_id)
            if size is None or size <= _MAX_AGGREGATE_RESTORE_BYTES:
                return True
            self.source_status[f"restore.{kind}"] = (
                f"skipped_oversize:{size}>{_MAX_AGGREGATE_RESTORE_BYTES}"
            )
            return False

        risk = (
            self.repository.get("risk_pattern", _RISK_STORE_ID)
            if within_restore_budget("risk_pattern", _RISK_STORE_ID)
            else None
        )
        if risk is not None:
            self.risks = RiskPatternStore.from_snapshot(risk.payload)
        feature = (
            self.repository.get("network_feature", _FEATURE_STORE_ID)
            if within_restore_budget("network_feature", _FEATURE_STORE_ID)
            else None
        )
        if feature is not None:
            self.features = NetworkFeatureEngine(FeatureStore.from_dict(feature.payload))
        before = self.features.store.dumps()
        now = _now()
        for dossier in restored_dossiers:
            self.features.update(dossier, now=max(now, dossier.updated_at))
        for pattern in self.risks.list_patterns():
            self.features.update(pattern, now=max(now, pattern.last_seen))
        if self.features.store.dumps() != before:
            self.repository.upsert(
                "network_feature", _FEATURE_STORE_ID, self.features.store.to_dict()
            )

    def _persist_derived(self) -> None:
        self.repository.upsert("risk_pattern", _RISK_STORE_ID, self.risks.snapshot())
        self.repository.upsert(
            "network_feature", _FEATURE_STORE_ID, self.features.store.to_dict()
        )

    def health_view(self) -> dict[str, Any]:
        """Cheap counters for health checks, without running aggregation queries."""
        with self._lock:
            return {
                "durable": self.durable,
                "last_refresh": self.last_refresh.isoformat() if self.last_refresh else None,
                "dossiers": len(self.dossiers.list()),
                "risk_patterns": len(self.risks.list_patterns()),
                "network_features": len(self.features.store.features()),
                "sources": dict(self.source_status),
            }

    @staticmethod
    def _dossier_payload(dossier: Any) -> dict[str, Any]:
        if isinstance(dossier, Mapping):
            payload = dict(dossier)
        elif callable(getattr(dossier, "to_dict", None)):
            payload = dict(dossier.to_dict())
        elif callable(getattr(dossier, "as_dict", None)):
            payload = dict(dossier.as_dict())
        elif callable(getattr(dossier, "model_dump", None)):
            payload = dict(dossier.model_dump(mode="json"))
        else:
            raise TypeError("dossier must be a mapping or expose to_dict()/as_dict()")
        return payload

    def save_dossier(self, dossier: Any) -> dict[str, Any]:
        """Persist a completed dossier and feed only its verified claims downstream."""
        payload = self._dossier_payload(dossier)
        dossier_id = str(
            payload.get("dossier_id") or payload.get("incident_id") or payload.get("id") or ""
        ).strip()
        if not dossier_id:
            raise ValueError("dossier id is required")
        with self._lock:
            if payload.get("schema_version") == "1.0":
                self.dossiers.ingest(IncidentDossier.model_validate(payload))
            self.repository.upsert("incident_dossier", dossier_id, payload)
            try:
                self.features.update(payload, now=_now())
            except (TypeError, ValueError):
                # Open/inconclusive dossiers are still authoritative records;
                # feature extraction correctly yields nothing for unsupported shapes.
                pass
            self._persist_derived()
        return payload

    def save_dossiers(self, dossiers: Iterable[Any]) -> list[dict[str, Any]]:
        """Persist a timeline batch and reconcile derived stores once."""
        saved: list[dict[str, Any]] = []
        with self._lock:
            for dossier in dossiers:
                payload = self._dossier_payload(dossier)
                dossier_id = str(
                    payload.get("dossier_id") or payload.get("incident_id")
                    or payload.get("id") or ""
                ).strip()
                if not dossier_id:
                    raise ValueError("dossier id is required")
                existing = self.dossiers.get(dossier_id)
                if existing is not None and existing.model_dump(mode="json") == payload:
                    continue
                if payload.get("schema_version") == "1.0":
                    self.dossiers.ingest(IncidentDossier.model_validate(payload))
                self.repository.upsert("incident_dossier", dossier_id, payload)
                try:
                    self.features.update(payload, now=_now())
                except (TypeError, ValueError):
                    pass
                saved.append(payload)
            if saved:
                self._persist_derived()
        return saved

    def ingest_risk_rows(
        self, rows: list[dict[str, Any]], *, source_table: str
    ) -> int:
        with self._lock:
            before = self.risks.snapshot_json()
            affected = self.risks.ingest_clickhouse_rows(rows, source_table=source_table)
            # The bounded source query intentionally overlaps the previous
            # window.  Most refreshes therefore replay the same event ids.  Do
            # not rebuild and rewrite thousands of derived features when the
            # authoritative risk store did not change.
            if self.risks.snapshot_json() == before:
                return 0
            self._update_affected_patterns(affected)
            self._persist_derived()
            return len(affected)

    def _update_affected_patterns(self, affected: Iterable[Any]) -> None:
        for pattern in affected:
            self.features.update(pattern, now=_now())
            if (
                pattern.provenance == "real"
                and pattern.status in {"active", "recurrent"}
                and pattern.risk_type in _INCIDENT_RISK_TYPES
                and pattern.event_count >= _INCIDENT_RISK_THRESHOLD
            ):
                dossier = from_risk_pattern(pattern)
                if self.dossiers.get(dossier.dossier_id) is None:
                    self.dossiers.ingest(dossier)
                    self.repository.upsert(
                        "incident_dossier", dossier.dossier_id,
                        dossier.model_dump(mode="json"),
                    )

    def attach_remediation_run(
        self, dossier_id: str, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Append a measured action/readback/watch result to an open dossier."""
        previous = self.dossiers.get(dossier_id)
        if previous is None:
            raise KeyError(dossier_id)
        if previous.status in {"resolved", "closed_false_positive"}:
            raise ValueError("cannot attach remediation to a closed dossier")
        document = json.dumps(
            dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        completed_at = datetime.fromisoformat(
            str(result.get("at") or _now().isoformat()).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        verdict = dict(result.get("verdict") or {})
        samples = [dict(item) for item in verdict.get("samples") or ()]
        window_seconds = max(0.0, float(verdict.get("window_seconds") or 0.0))
        started_at = completed_at - timedelta(seconds=window_seconds)
        action = str(result.get("action") or verdict.get("action") or "").strip()
        target = str(result.get("target") or "").strip()
        if not action or not target:
            raise ValueError("remediation result requires action and target")
        raw_outcome = str(verdict.get("outcome") or result.get("outcome") or "").lower()
        outcome = {
            "passed": "succeeded",
            "reverted": "rolled_back",
            "revert_unverified": "failed",
            "not_committed": "failed",
        }.get(raw_outcome, "failed")

        receipt_evidence_id = f"remediation:{digest}:receipt"
        added_evidence = [EvidenceReference(
            evidence_id=receipt_evidence_id,
            source_type="action_receipt",
            locator=f"remediation-run:{digest}",
            observed_at=completed_at,
            summary=f"{action} on {target}: {raw_outcome or outcome}",
            content_sha256=digest,
        )]
        checks: list[ReadbackCheck] = []
        sample_evidence_ids: list[str] = []
        for index, sample in enumerate(samples, start=1):
            at = datetime.fromisoformat(
                str(sample.get("at") or completed_at.isoformat()).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            sample_json = json.dumps(
                sample, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            )
            sample_id = f"remediation:{digest}:sample:{index:04d}"
            sample_evidence_ids.append(sample_id)
            added_evidence.append(EvidenceReference(
                evidence_id=sample_id,
                source_type="readback",
                locator=f"remediation-run:{digest}:sample:{index}",
                observed_at=at,
                summary=f"{sample.get('probe') or 'probe'} readback",
                content_sha256=hashlib.sha256(sample_json.encode("utf-8")).hexdigest(),
            ))
            checks.append(ReadbackCheck(
                name=f"{sample.get('probe') or 'probe'}:{index}",
                passed=not bool(sample.get("regressed")),
                observed_value=bool(sample.get("healthy")),
                evidence_id=sample_id,
            ))
        if not checks:
            checks.append(ReadbackCheck(
                name="watch_window_verdict",
                passed=raw_outcome == "passed",
                observed_value=raw_outcome,
                evidence_id=receipt_evidence_id,
            ))
        readback = ReadbackResult(
            readback_id=f"readback:{digest}",
            collected_at=completed_at,
            verdict="passed" if all(item.passed for item in checks) else "failed",
            checks=tuple(checks),
        )
        observation = ObservationWindow(
            started_at=started_at,
            planned_end_at=started_at + timedelta(seconds=window_seconds),
            verdict="passed" if raw_outcome == "passed" else "failed",
            completed_at=completed_at,
            hold_duration_seconds=window_seconds,
            evidence_ids=tuple(sample_evidence_ids or (receipt_evidence_id,)),
        )
        attempt = RemediationAttempt(
            attempt_id=f"attempt:{digest}",
            action=action,
            target_asset_id=target,
            initiated_at=started_at,
            outcome=outcome,
            precondition_evidence_ids=(receipt_evidence_id,),
            receipt=ActionReceipt(
                receipt_id=f"receipt:{digest}",
                executor="remediation-api",
                started_at=started_at,
                completed_at=completed_at,
                outcome=outcome,
                evidence_ids=(receipt_evidence_id,),
            ),
            readbacks=(readback,),
            observation=observation,
        )
        next_status = "resolved" if raw_outcome == "passed" else "escalated"
        payload = previous.model_dump(mode="json")
        payload.update({
            "status": next_status,
            "updated_at": completed_at.isoformat(),
            "closed_at": completed_at.isoformat() if next_status == "resolved" else None,
            "evidence": [*payload["evidence"], *(item.model_dump(mode="json") for item in added_evidence)],
            "remediation_attempts": [
                *payload["remediation_attempts"], attempt.model_dump(mode="json")
            ],
        })
        return self.save_dossier(IncidentDossier.model_validate(payload))

    def refresh(self) -> dict[str, Any]:
        with self._refresh_lock:
            return self._refresh_once()

    def _refresh_once(self) -> dict[str, Any]:
        """Read bounded real source windows and merge them idempotently."""
        query = self._query
        if query is None:
            from .history import _CH_DB, _q

            query = _q
            database = _CH_DB
        else:
            database = "netops"
        source_status: dict[str, str] = {}
        updated = 0
        risk_events: list[RiskEvent] = []

        source_queries = (
            (
                f"{database}.security_events",
                "SELECT event_ts, event_id, if(device_id='', device_name, device_id) AS device_key, "
                "srcip, dstip, dstport, action, method AS service, "
                "'event' AS type, event_type AS subtype, username AS user, status, "
                "logdesc, message AS msg, event_type AS risk_type, provenance "
                f"FROM {database}.security_events "
                "WHERE event_ts >= now() - INTERVAL 90 DAY "
                "ORDER BY event_ts DESC LIMIT 20000",
            ),
            (
                f"{database}.facts",
                "SELECT event_ts, device_key, srcip, dstip, dstport, proto, action, "
                "service, app, type, subtype "
                f"FROM {database}.facts "
                "WHERE event_ts >= now() - INTERVAL 90 DAY AND action IN ('deny','blocked','block') "
                "ORDER BY event_ts DESC LIMIT 10000",
            ),
        )
        for source_table, sql in source_queries:
            try:
                rows = query(sql)
                risk_events.extend(
                    event
                    for event in (
                        risk_event_from_clickhouse_row(row, source_table=source_table)
                        for row in rows
                    )
                    if event is not None
                )
                source_status[source_table] = f"ok:{len(rows)}"
            except Exception as error:  # source health is returned, never hidden
                source_status[source_table] = f"error:{type(error).__name__}"

        try:
            from core.remediate.sentinel import timeline
            from domains.network_rca.incident_dossier import from_sentinel_chain
            from domains.network_rca.incident_memory import completed_incident_chains

            rows = self._timeline_reader() if self._timeline_reader is not None else timeline(2000)
            chains = completed_incident_chains(rows)
            # One timeline read is one repository transaction boundary.  A
            # per-dossier save serializes and writes the complete risk and
            # feature snapshots once per historical incident, even when every
            # dossier already exists.  Batch comparison keeps an idle refresh
            # cheap and persists derived state once when something changed.
            self.save_dossiers(
                from_sentinel_chain(chain, source_mode="live") for chain in chains
            )
            source_status["sentinel.timeline"] = f"ok:{len(chains)}"
        except Exception as error:
            source_status["sentinel.timeline"] = f"error:{type(error).__name__}"

        try:
            if self._environment_reader is None:
                from domains.network_rca.environment import build_environment_report

                environment = build_environment_report()
            else:
                environment = self._environment_reader()
            checked_at = datetime.fromisoformat(
                str(environment["checked_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            environment_events: list[RiskEvent] = []
            for finding in environment.get("findings") or ():
                verification = dict(finding.get("verification") or {})
                if verification.get("state") != "confirmed":
                    continue
                finding_id = str(finding.get("finding_id") or "").strip()
                subject = str(finding.get("subject") or "").strip()
                fault_class = str(finding.get("fault_class") or "").strip()
                if not finding_id or not subject or not fault_class:
                    continue
                environment_events.append(RiskEvent(
                    event_id=f"environment:{finding_id}:{checked_at.date().isoformat()}",
                    observed_at=checked_at,
                    risk_type=fault_class,
                    scope_key=subject,
                    target_asset=subject,
                    provenance="real",
                    evidence_ref=f"environment.findings:{finding_id}",
                    source_table="environment.findings",
                ))
            risk_events.extend(environment_events)
            source_status["environment.findings"] = f"ok:{len(environment_events)}"
        except Exception as error:
            source_status["environment.findings"] = f"error:{type(error).__name__}"

        # Apply the complete source cut under one capacity decision.  This
        # prevents security events, flow facts and environment findings from
        # evicting one another at the bounded-store edge, and it writes one
        # authoritative risk/feature snapshot per refresh.
        try:
            with self._lock:
                before = self.risks.snapshot_json()
                affected = self.risks.ingest_many(risk_events)
                changed = self.risks.snapshot_json() != before
                if changed:
                    self._update_affected_patterns(affected)
                    self._persist_derived()
                updated += len(affected) if changed else 0
            source_status["feature.risk_reconciliation"] = f"ok:{len(affected)}"
        except Exception as error:
            source_status["feature.risk_reconciliation"] = f"error:{type(error).__name__}"

        # Derived features are updated in the same critical section as every
        # changed dossier, risk pattern and environment finding above.  A full
        # sweep here only reparses unchanged sources and rewrites the same large
        # snapshot.  Time-based feature reassessment remains part of ranked
        # investigation reads, where its decision is actually consumed.
        source_status["feature.reconciliation"] = "ok:source-driven"

        with self._lock:
            self.last_refresh = _now()
            self.source_status = source_status
        return {
            "ok": True,
            "updated_patterns": updated,
            "last_refresh": _iso(self.last_refresh),
            "sources": dict(source_status),
        }

    def recall(
        self,
        *,
        subject: str | None,
        family: str | None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Return historical priors with their status and sample boundaries."""
        at = _now()
        with self._lock:
            before = {
                feature.feature_id: (
                    feature.state,
                    len(self.features.store.decisions_for(feature.feature_id)),
                )
                for feature in self.features.store.features()
            }
            matches = self.features.rank_for_investigation(
                at=at,
                asset_ids=(subject,) if subject else (),
                fault_family=family,
                limit=limit,
            )
            after = {
                feature.feature_id: (
                    feature.state,
                    len(self.features.store.decisions_for(feature.feature_id)),
                )
                for feature in self.features.store.features()
            }
            if after != before:
                self.repository.upsert(
                    "network_feature", _FEATURE_STORE_ID, self.features.store.to_dict()
                )
            risks = (
                self.risks.search(subject, limit=limit)
                if subject else self.risks.list_patterns(limit=limit)
            )
        dossiers = []
        for snapshot in self.repository.load("incident_dossier"):
            row = snapshot.payload
            haystack = " ".join(
                str(value) for value in (
                    row.get("subject"), row.get("asset"), row.get("affected_assets"),
                    row.get("asset_ids"),
                    row.get("family"), row.get("fault_family"), row.get("root_cause"),
                )
            )
            if subject and subject not in haystack:
                continue
            if family and family not in haystack:
                continue
            dossiers.append(row)
        dossiers = dossiers[-limit:]
        return {
            "historical_only": True,
            "dossiers": dossiers,
            "risks": [row.to_dict() for row in risks],
            "features": [row.to_dict() for row in matches],
        }

    @staticmethod
    def _dossier_row(payload: Mapping[str, Any]) -> dict[str, Any]:
        evidence = payload.get("evidence") or payload.get("evidence_refs") or ()
        root = payload.get("root_cause")
        if root is None:
            candidates = payload.get("root_causes") or ()
            if isinstance(candidates, (list, tuple)) and candidates:
                root = candidates[0]
        if isinstance(root, Mapping):
            root_text = str(
                root.get("statement") or root.get("key") or root.get("name") or ""
            )
        else:
            root_text = str(root or "")
        return {
            "id": str(payload.get("dossier_id") or payload.get("incident_id") or payload.get("id")),
            "title": str(
                payload.get("fault_summary") or payload.get("summary")
                or payload.get("title") or root_text or "incident"
            ),
            "status": str(payload.get("status") or payload.get("resolution") or "open"),
            "source": str(
                payload.get("source_mode") or payload.get("provenance")
                or payload.get("source") or "real"
            ),
            "first_seen": payload.get("opened_at") or payload.get("first_seen"),
            "last_seen": payload.get("closed_at") or payload.get("last_seen") or payload.get("updated_at"),
            "evidence_count": len(evidence) if isinstance(evidence, (list, tuple)) else 0,
            "reason": root_text or None,
        }

    def audit_view(self, *, subject: str | None = None) -> dict[str, Any]:
        recalled = self.recall(subject=subject, family=None, limit=100)
        dossiers = [self._dossier_row(row) for row in recalled["dossiers"]]
        risks = []
        for row in recalled["risks"]:
            matched_on: list[str] = []
            if subject:
                if subject in row.get("target_accounts", ()):
                    matched_on.append("target_account")
                if subject in row.get("target_assets", ()):
                    matched_on.append("target_asset")
                if subject in row.get("source_ips", ()):
                    matched_on.append("source_ip")
                if subject in row.get("source_networks", ()):
                    matched_on.append("source_network")
                if subject == row.get("scope_key"):
                    matched_on.append("scope")
            risks.append({
                "id": row["pattern_id"],
                "title": row["risk_type"],
                "status": row["status"],
                "source": row["provenance"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "evidence_count": row["event_count"],
                "reason": f"{row['active_days']} active days · {row['trend']}",
                "scope": row["scope_key"],
                "target_account_count": len(row.get("target_accounts", ())),
                "matched_on": matched_on,
            })
        features = []
        for feature in self.features.store.features():
            if subject and feature.scope.asset_ids and subject not in feature.scope.asset_ids:
                continue
            decisions = self.features.store.decisions_for(feature.feature_id)
            reason = ", ".join(decisions[-1].reason_codes) if decisions else "no decision"
            features.append({
                "id": feature.feature_id,
                "title": feature.statement,
                "status": feature.state,
                "source": feature.signal,
                "first_seen": _iso(feature.valid_from),
                "last_seen": _iso(feature.last_verified),
                "sample_count": feature.sample_size,
                "confidence": feature.confidence,
                "reason": reason,
            })
        features.sort(key=lambda row: (row["status"] != "promoted", -row["confidence"], row["id"]))
        good_sources = [
            key for key, value in self.source_status.items()
            if value.startswith("ok:") and not key.startswith("feature.")
        ]
        gaps = [
            f"{key} unavailable ({value.split(':', 1)[-1]})"
            for key, value in self.source_status.items() if value.startswith("error:")
        ]
        if not self.durable:
            gaps.append("operational records are using process-local fallback storage")
        if not self.source_status:
            gaps.append("real source refresh has not run in this process")
        gaps.append(
            "remote firewalls and switches have read-only evidence adapters; "
            "approved write adapters and device-specific rollback contracts are not configured"
        )
        return {
            "ok": True,
            "durable": self.durable,
            "last_refresh": _iso(self.last_refresh),
            "coverage": {"sources": good_sources, "blind_spots": gaps},
            "action_scope": {
                "automatic": ["local failed service", "local addressed NIC without carrier"],
                "escalation_only": ["remote firewall", "remote switch", "remote endpoint"],
            },
            "components": {
                "feature_reconciliation": self.source_status.get(
                    "feature.reconciliation", "not_run"
                )
            },
            "counts": {
                "dossiers": len(dossiers), "risks": len(risks), "features": len(features),
            },
            "dossiers": dossiers,
            "risks": risks,
            "features": features,
        }

    def incident_receipt_view(
        self, *, subject: str, dossier_id: str | None,
    ) -> dict[str, Any]:
        """Exact dossier lookup for one selected live incident.

        Risk and feature aggregation have their own whole-network surface. The
        event receipt stays on the deterministic chain key and never waits for
        a broad source refresh holding the service orchestration lock.
        """
        dossier = self.dossiers.get(dossier_id) if dossier_id else None
        dossiers = [
            self._dossier_row(dossier.model_dump(mode="json"))
        ] if dossier is not None else []
        return {
            "ok": True,
            "durable": self.durable,
            "dossiers": dossiers,
            "risks": [],
            "features": [],
        }


def build_operational_memory_service() -> OperationalMemoryService:
    """Prefer PostgreSQL and report a truthful degraded mode when unavailable."""
    dsn = autopoiesis_env("MEMORY_DSN")
    if dsn:
        try:
            repository = PostgresOperationalRepository(dsn)
            repository.initialize_schema()
            return OperationalMemoryService(repository, durable=True)
        except Exception:
            pass
    return OperationalMemoryService(InMemoryOperationalRepository(), durable=False)


__all__ = ["OperationalMemoryService", "build_operational_memory_service"]
