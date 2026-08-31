from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.memory.operational_repository import InMemoryOperationalRepository
from frontend.gateway.app.operational_memory import OperationalMemoryService


NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def _dossier(index: int) -> dict:
    observed = NOW + timedelta(minutes=index)
    return {
        "dossier_id": f"incident-{index}",
        "verified": True,
        "status": "confirmed",
        "summary": "gateway carrier failure",
        "affected_assets": ["192.168.1.27"],
        "fault_family": "fam-host-config-drift",
        "scope": {
            "asset_ids": ["192.168.1.27"],
            "roles": ["gateway"],
            "fault_family": "fam-host-config-drift",
            "config_version": "network-v7",
        },
        "metric_window": {
            "metric": "carrier_state",
            "aggregation": "last",
            "duration_seconds": 300,
        },
        "root_cause": {
            "key": "carrier_down",
            "status": "confirmed",
            "confidence": 1.0,
            "evidence_refs": [f"ev-{index}"],
        },
        "observed_at": observed.isoformat(),
        "opened_at": (observed - timedelta(minutes=5)).isoformat(),
        "closed_at": observed.isoformat(),
        "evidence_refs": [f"ev-{index}"],
    }


def test_verified_independent_dossiers_promote_and_persist_a_feature():
    repository = InMemoryOperationalRepository()
    service = OperationalMemoryService(repository, durable=False)

    for index in range(3):
        service.save_dossier(_dossier(index))

    view = service.audit_view(subject="192.168.1.27")
    assert view["counts"] == {"dossiers": 3, "risks": 0, "features": 1}
    assert view["features"][0]["status"] == "promoted"
    assert view["features"][0]["sample_count"] == 3

    restored = OperationalMemoryService(repository, durable=False)
    restored_view = restored.audit_view(subject="192.168.1.27")
    assert restored_view["features"][0]["id"] == view["features"][0]["id"]
    assert restored_view["features"][0]["status"] == "promoted"
    assert restored_view["features"][0]["sample_count"] == 3


def test_refresh_merges_security_and_deny_sources_and_reports_source_health():
    def query(sql: str) -> list[dict]:
        if "security_events" in sql:
            return [{
                "event_ts": NOW.isoformat(),
                "event_id": "security-1",
                "device_key": "edge-fw",
                "srcip": "198.51.100.9",
                "dstip": "192.168.1.1",
                "action": "login",
                "status": "failed",
                "risk_type": "admin_login_failed",
                "user": "admin",
                "provenance": "real",
            }]
        return [{
            "event_ts": (NOW + timedelta(seconds=1)).isoformat(),
            "device_key": "192.168.1.27",
            "srcip": "192.168.1.27",
            "dstip": "203.0.113.8",
            "dstport": 37777,
            "proto": "6",
            "action": "deny",
            "provenance": "real",
        }]

    service = OperationalMemoryService(
        InMemoryOperationalRepository(), query=query, timeline_reader=lambda: [],
        environment_reader=lambda: {"checked_at": NOW.isoformat(), "findings": []},
        durable=False,
    )
    result = service.refresh()

    assert result["ok"] is True
    assert result["updated_patterns"] == 2
    view = service.audit_view()
    assert view["counts"]["risks"] == 2
    assert set(view["coverage"]["sources"]) == {
        "environment.findings", "autopoiesis.facts",
        "autopoiesis.security_events", "sentinel.timeline"
    }
    assert all(row["source"] == "real" for row in view["risks"])
    assert service.refresh()["updated_patterns"] == 0


def test_repeated_real_security_risk_opens_one_durable_dossier():
    rows = [
        {
            "event_ts": (NOW + timedelta(seconds=index)).isoformat(),
            "event_id": f"security-{index}",
            "device_key": "edge-fw",
            "srcip": f"198.51.100.{index + 1}",
            "dstip": "192.168.1.1",
            "action": "login",
            "status": "failed",
            "risk_type": "admin_login_failed",
            "user": "mike",
            "provenance": "real",
        }
        for index in range(20)
    ]
    repository = InMemoryOperationalRepository()
    service = OperationalMemoryService(repository, durable=False)

    assert service.ingest_risk_rows(rows, source_table="autopoiesis.security_events") == 1
    view = service.audit_view()

    assert view["counts"]["dossiers"] == 1
    assert view["dossiers"][0]["status"] == "open"
    assert "causal confirmation" in view["dossiers"][0]["reason"]
    assert repository.load("incident_dossier")[0].payload["root_causes"][0]["status"] == "hypothesis"

    matched = service.audit_view(subject="mike")
    assert matched["risks"][0]["matched_on"] == ["target_account"]
    assert matched["risks"][0]["scope"] == "edge-fw"


def test_unavailable_source_is_exposed_as_a_coverage_gap():
    def query(sql: str) -> list[dict]:
        if "security_events" in sql:
            raise TimeoutError("collector unavailable")
        return []

    service = OperationalMemoryService(
        InMemoryOperationalRepository(), query=query, timeline_reader=lambda: [],
        environment_reader=lambda: {"checked_at": NOW.isoformat(), "findings": []},
        durable=False,
    )
    service.refresh()
    gaps = service.audit_view()["coverage"]["blind_spots"]

    assert any("security_events unavailable (TimeoutError)" in gap for gap in gaps)
    assert any("process-local fallback" in gap for gap in gaps)


def test_refresh_backfills_completed_sentinel_chain_into_dossier():
    rows = [
        {
            "at": NOW.isoformat(),
            "kind": "detected",
            "detector": "remote_policy_deny",
            "family": "fam-policy-reachability",
            "subject": "192.168.1.88",
            "severity": "high",
            "summary": "policy denies persist",
            "action": None,
        },
        {
            "at": (NOW + timedelta(seconds=20)).isoformat(),
            "kind": "no_safe_action",
            "detector": "remote_policy_deny",
            "subject": "192.168.1.88",
            "action": "",
            "reason": "remote adapter is read-only",
        },
    ]
    service = OperationalMemoryService(
        InMemoryOperationalRepository(),
        query=lambda sql: [],
        timeline_reader=lambda: rows,
        environment_reader=lambda: {"checked_at": NOW.isoformat(), "findings": []},
        durable=False,
    )

    service.refresh()

    dossiers = service.repository.load("incident_dossier")
    assert len(dossiers) == 1
    assert dossiers[0].payload["asset_ids"] == ["192.168.1.88"]
    assert dossiers[0].payload["root_causes"][0]["status"] == "hypothesis"
    assert service.source_status["sentinel.timeline"] == "ok:1"


def test_environment_refresh_keeps_only_current_confirmed_structural_risks():
    environment = {
        "checked_at": NOW.isoformat(),
        "findings": [
            {
                "finding_id": "duplicate-23",
                "subject": "192.168.1.23",
                "fault_class": "duplicate_ip_static",
                "verification": {"state": "confirmed"},
            },
            {
                "finding_id": "unverified-churn",
                "subject": "192.168.16.0/24",
                "fault_class": "lease_churn",
                "verification": {"state": "unverifiable"},
            },
        ],
    }
    service = OperationalMemoryService(
        InMemoryOperationalRepository(),
        query=lambda sql: [],
        timeline_reader=lambda: [],
        environment_reader=lambda: environment,
        durable=False,
    )

    service.refresh()
    risks = service.audit_view()["risks"]

    assert [row["title"] for row in risks] == ["duplicate_ip_static"]
    assert risks[0]["evidence_count"] == 1


def test_health_view_reports_stored_objects_without_refreshing_sources():
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)

    health = service.health_view()

    assert health == {
        "durable": False,
        "last_refresh": None,
        "dossiers": 0,
        "risk_patterns": 0,
        "network_features": 0,
        "sources": {},
    }


def test_oversize_aggregate_snapshots_are_rebuilt_instead_of_restored():
    class OversizeRepository(InMemoryOperationalRepository):
        def payload_size(self, kind, record_id):
            if kind in {"risk_pattern", "network_feature"}:
                return 9 * 1024 * 1024
            return super().payload_size(kind, record_id)

        def get(self, kind, record_id):
            if kind in {"risk_pattern", "network_feature"}:
                raise AssertionError("oversize aggregate payload must not be decoded")
            return super().get(kind, record_id)

    service = OperationalMemoryService(OversizeRepository(), durable=False)

    assert service.health_view()["sources"] == {
        "restore.network_feature": "skipped_oversize:9437184>8388608",
        "restore.risk_pattern": "skipped_oversize:9437184>8388608",
    }
