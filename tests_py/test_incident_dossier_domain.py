from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from domains.network_rca.incident_dossier import (
    ActionReceipt,
    DossierConflictError,
    EvidenceReference,
    IncidentDossier,
    InMemoryDossierStore,
    ObservationWindow,
    ReadbackCheck,
    ReadbackResult,
    RemediationAttempt,
    RootCauseHypothesis,
    from_risk_pattern,
    from_sentinel_chain,
)
from domains.network_rca.risk_pattern import RiskEvent, RiskPatternStore


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def evidence(evidence_id: str, seconds: int = 0) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type="telemetry",
        locator=f"clickhouse://network_events/{evidence_id}",
        observed_at=NOW + timedelta(seconds=seconds),
        summary=f"captured fact {evidence_id}",
        content_sha256="a" * 64,
    )


def dossier(**updates) -> IncidentDossier:
    values = {
        "dossier_id": "inc-001",
        "source_mode": "live",
        "status": "open",
        "fault_family": "service_unavailable",
        "fault_summary": "API health probe failed",
        "severity": "high",
        "symptom_fingerprint": "api-health-v1",
        "asset_ids": ("api-02", "api-01"),
        "opened_at": NOW,
        "updated_at": NOW,
        "evidence": (evidence("ev-2", 2), evidence("ev-1", 1)),
    }
    values.update(updates)
    return IncidentDossier(**values)


def successful_attempt() -> RemediationAttempt:
    receipt = ActionReceipt(
        receipt_id="receipt-1",
        executor="remote-tool-r230",
        started_at=NOW + timedelta(seconds=10),
        completed_at=NOW + timedelta(seconds=20),
        outcome="succeeded",
        evidence_ids=("ev-action",),
    )
    return RemediationAttempt(
        attempt_id="attempt-1",
        action="restart_service",
        target_asset_id="api-01",
        initiated_at=NOW + timedelta(seconds=10),
        outcome="succeeded",
        precondition_evidence_ids=("ev-1",),
        receipt=receipt,
        readbacks=(
            ReadbackResult(
                readback_id="readback-1",
                collected_at=NOW + timedelta(seconds=30),
                verdict="passed",
                checks=(
                    ReadbackCheck(
                        name="service_healthy",
                        passed=True,
                        observed_value=True,
                        evidence_id="ev-readback",
                    ),
                ),
            ),
        ),
        observation=ObservationWindow(
            started_at=NOW + timedelta(seconds=20),
            planned_end_at=NOW + timedelta(seconds=80),
            verdict="passed",
            completed_at=NOW + timedelta(seconds=80),
            hold_duration_seconds=60,
            evidence_ids=("ev-observation",),
        ),
    )


def test_dossier_serialization_is_deterministic_for_set_like_fields() -> None:
    first = dossier()
    second = dossier(
        asset_ids=("api-01", "api-02", "api-01"),
        evidence=(evidence("ev-1", 1), evidence("ev-2", 2)),
    )

    assert first.asset_ids == ("api-01", "api-02")
    assert [item.evidence_id for item in first.evidence] == ["ev-1", "ev-2"]
    assert first.canonical_json() == second.canonical_json()
    assert first.content_digest == second.content_digest


def test_detector_can_propose_but_cannot_confirm_a_root_cause() -> None:
    with pytest.raises(ValidationError, match="cannot be confirmed directly"):
        RootCauseHypothesis(
            hypothesis_id="root-1",
            statement="Service failed because its dependency was unreachable",
            status="confirmed",
            origin="detector",
            detector_id="failed_units",
            confidence=0.99,
            evidence_ids=("ev-1",),
            updated_at=NOW,
            confirmed_by="operator:alice",
        )

    confirmed = RootCauseHypothesis(
        hypothesis_id="root-1",
        statement="Service failed because its dependency was unreachable",
        status="confirmed",
        origin="operator",
        confidence=0.99,
        evidence_ids=("ev-1",),
        updated_at=NOW,
        confirmed_by="operator:alice",
    )
    assert confirmed.status == "confirmed"


def test_real_risk_pattern_opens_unconfirmed_investigation_dossier() -> None:
    store = RiskPatternStore()
    pattern = store.ingest(
        RiskEvent(
            event_id="security-1",
            observed_at=NOW,
            risk_type="admin_login_failed",
            scope_key="edge-fw",
            target_asset="192.168.1.1",
            target_account="mike",
            source_ip="198.51.100.7",
            provenance="real",
            evidence_ref="event_id=security-1",
            source_table="netops.security_events",
        )
    )
    assert pattern is not None

    opened = from_risk_pattern(pattern)

    assert opened.status == "open"
    assert opened.source_mode == "live"
    assert opened.asset_ids == ("192.168.1.1",)
    assert opened.root_causes[0].status == "hypothesis"
    assert opened.root_causes[0].origin == "detector"
    assert opened.evidence[0].locator.startswith("risk-pattern:")
    assert pattern.pattern_id in opened.evidence[0].locator


def test_replay_risk_cannot_open_production_dossier() -> None:
    pattern = RiskPatternStore().ingest(
        RiskEvent(
            event_id="replay-1",
            observed_at=NOW,
            risk_type="admin_login_failed",
            scope_key="edge-fw",
            target_asset="192.168.1.1",
            provenance="replay",
        )
    )
    assert pattern is not None

    with pytest.raises(ValueError, match="only real risk patterns"):
        from_risk_pattern(pattern)


def test_every_nested_reference_must_resolve_to_dossier_evidence() -> None:
    root = RootCauseHypothesis(
        hypothesis_id="root-1",
        statement="Dependency endpoint refused connections",
        status="supported",
        origin="analysis",
        confidence=0.7,
        evidence_ids=("missing",),
        updated_at=NOW,
    )
    with pytest.raises(ValidationError, match="unknown evidence references: missing"):
        dossier(root_causes=(root,))


def test_resolved_requires_action_receipt_readback_observation_and_hold_duration() -> None:
    with pytest.raises(ValidationError, match="successful action"):
        dossier(status="resolved", closed_at=NOW + timedelta(minutes=2))

    full_evidence = (
        evidence("ev-1", 1),
        evidence("ev-2", 2),
        evidence("ev-action", 20),
        evidence("ev-readback", 30),
        evidence("ev-observation", 80),
    )
    resolved = dossier(
        status="resolved",
        updated_at=NOW + timedelta(seconds=80),
        closed_at=NOW + timedelta(seconds=80),
        evidence=full_evidence,
        remediation_attempts=(successful_attempt(),),
    )
    assert resolved.remediation_attempts[0].observation.hold_duration_seconds == 60


def test_store_ingest_is_idempotent_and_versions_real_changes() -> None:
    store = InMemoryDossierStore()
    initial = dossier()

    first = store.ingest(initial)
    duplicate = store.ingest(dossier(evidence=tuple(reversed(initial.evidence))))
    investigating = initial.transition_to("investigating", at=NOW + timedelta(seconds=5))
    changed = store.ingest(investigating)

    assert (first.version, first.written) == (1, True)
    assert (duplicate.version, duplicate.written) == (1, False)
    assert (changed.version, changed.written) == (2, True)
    assert store.get("inc-001").status == "investigating"


def test_store_rejects_status_regression_and_evidence_rewrite() -> None:
    store = InMemoryDossierStore()
    initial = dossier()
    investigating = initial.transition_to("investigating", at=NOW + timedelta(seconds=5))
    store.ingest(investigating)

    with pytest.raises(ValueError, match="illegal dossier status transition"):
        store.ingest(
            dossier(updated_at=NOW + timedelta(seconds=10), status="open")
        )

    changed_evidence = EvidenceReference(
        **{
            **evidence("ev-1", 1).model_dump(),
            "summary": "rewritten after acceptance",
        }
    )
    with pytest.raises(DossierConflictError, match="accepted evidence changed"):
        store.ingest(
            investigating.model_copy(
                update={
                    "updated_at": NOW + timedelta(seconds=10),
                    "evidence": (changed_evidence, evidence("ev-2", 2)),
                }
            )
        )


def test_store_searches_by_asset_family_and_exact_symptom_fingerprint() -> None:
    store = InMemoryDossierStore(
        [
            dossier(),
            dossier(
                dossier_id="inc-002",
                fault_family="link_down",
                symptom_fingerprint="carrier-lost-v2",
                asset_ids=("switch-01",),
            ),
        ]
    )

    assert [item.dossier_id for item in store.search(asset_id="api-01")] == ["inc-001"]
    assert [item.dossier_id for item in store.search(fault_family="link_down")] == ["inc-002"]
    assert [
        item.dossier_id
        for item in store.search(symptom_fingerprint="api-health-v1")
    ] == ["inc-001"]
    assert store.search(asset_id="api-01", fault_family="link_down") == []
    with pytest.raises(ValueError, match="requires"):
        store.search()


def sentinel_chain(terminal: str) -> list[dict]:
    detected = {
        "at": NOW.isoformat(),
        "kind": "detected",
        "detector": "failed_units",
        "family": "service_health",
        "subject": "api.service",
        "action": None if terminal == "no_safe_action" else "restart_unit",
        "severity": "high",
        "summary": "api.service is failed",
    }
    terminal_row = {
        "at": (NOW + timedelta(seconds=1)).isoformat(),
        "kind": terminal,
        "subject": "api.service",
        "detector": "failed_units",
        "action": detected["action"],
        "reason": f"terminal {terminal}",
    }
    return [detected, terminal_row]


@pytest.mark.parametrize(
    ("terminal", "status", "attempt_outcome"),
    [
        ("declined", "open", "declined"),
        ("escalated", "escalated", "failed"),
        ("no_safe_action", "open", None),
    ],
)
def test_sentinel_non_success_outcomes_become_auditable_dossiers(
    terminal: str, status: str, attempt_outcome: str | None
) -> None:
    built = from_sentinel_chain(sentinel_chain(terminal))

    assert built.status == status
    assert built.fault_family == "failed_units"
    assert built.root_causes[0].status == "hypothesis"
    assert built.root_causes[0].origin == "detector"
    if attempt_outcome is None:
        assert built.remediation_attempts == ()
    else:
        assert built.remediation_attempts[0].outcome == attempt_outcome


def test_passed_sentinel_chain_keeps_receipt_readback_and_observation() -> None:
    chain = sentinel_chain("resolved")
    chain[-1]["outcome"] = "passed"
    built = from_sentinel_chain(chain, source_mode="drill")

    assert built.source_mode == "drill"
    assert built.status == "resolved"
    attempt = built.remediation_attempts[0]
    assert attempt.receipt.outcome == "succeeded"
    assert attempt.readbacks[0].verdict == "passed"
    assert attempt.observation.verdict == "passed"
    assert attempt.observation.hold_duration_seconds == 1
    assert built.root_causes[0].status == "hypothesis"


def test_sentinel_dossier_plans_both_fast_and_stability_windows() -> None:
    chain = sentinel_chain("resolved")
    chain[-1]["outcome"] = "passed"
    chain[-1]["at"] = (NOW + timedelta(seconds=241)).isoformat()
    chain.insert(
        1,
        {
            "at": (NOW + timedelta(seconds=1)).isoformat(),
            "kind": "bakein_opened",
            "subject": "api.service",
            "action": "restart_unit",
            "window_seconds": 60,
            "stability_window_seconds": 180,
        },
    )

    attempt = from_sentinel_chain(chain).remediation_attempts[0]

    assert attempt.observation.planned_end_at == NOW + timedelta(seconds=241)
    assert attempt.observation.hold_duration_seconds == 240
