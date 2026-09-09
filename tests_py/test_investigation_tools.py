from __future__ import annotations

from frontend.gateway.app import history
from frontend.gateway.app.investigation_tools import (
    collect_admin_auth_window,
    collect_fortigate_context,
    collect_fortigate_current_status,
    resolve_device_subject,
)


class _API:
    def fetch_all(self):
        base = {"available": True, "degraded": False, "missing": []}
        return {
            "fetched_at": "2026-08-29T20:00:00Z",
            "degraded": False,
            "interfaces": {
                **base,
                "items": [{"name": "office", "ip": "192.168.16.1/24"}],
            },
            "devices": {
                **base,
                "items": [
                    {"ip": "192.168.16.56", "mac": "aa:bb", "hostname": "camera"},
                    {"ip": "192.168.1.20", "mac": "cc:dd", "hostname": "printer"},
                ],
            },
            "policies": {**base, "items": [{"id": 41, "action": "deny"}]},
            "changes": {**base, "items": [{"version": 7, "administrator": "ops"}]},
        }

    def fetch_devices(self):
        result = self.fetch_all()["devices"]
        return {**result, "fetched_at": "2026-08-29T20:00:00Z"}

    def fetch_identity(self):
        return {
            "available": True,
            "degraded": False,
            "items": [{
                "vendor": "Fortinet", "product": "FortiGate",
                "model": "FG100E", "firmware_version": "v6.2.7",
            }],
        }


def _factory(**_options):
    return _API()


def test_subject_context_keeps_router_identity_and_current_sessions() -> None:
    result = collect_fortigate_context(
        "192.168.16.56",
        api_factory=_factory,
        device_status_reader=lambda ip, lang: {
            "state": "online", "ip": ip, "language": lang
        },
        live_flow_reader=lambda ip: {"sessionCount": 3, "subject": ip},
    )

    assert result["inventory_count"] == 2
    assert result["target_devices"] == [
        {"ip": "192.168.16.56", "mac": "aa:bb", "hostname": "camera"}
    ]
    assert result["target_status"]["state"] == "online"
    assert result["target_flows"]["sessionCount"] == 3
    assert result["interfaces"][0]["name"] == "office"
    assert result["policies"][0]["id"] == 41
    assert result["sourceRole"] == "current_state_readback"
    assert result["freshness"]["direct"] is True


def test_non_ip_subject_does_not_request_host_session_data() -> None:
    def fail(*_args):
        raise AssertionError("host-specific reader should not run")

    result = collect_fortigate_context(
        "autopoiesis-gateway.service",
        api_factory=_factory,
        device_status_reader=fail,
        live_flow_reader=fail,
    )

    assert result["target_devices"] == []
    assert "target_status" not in result
    assert "target_flows" not in result


def test_inventory_hostname_in_natural_language_resolves_to_ip() -> None:
    result = resolve_device_subject("请看 camera 当前在线状态", api_factory=_factory)

    assert result == {
        "subject": "192.168.16.56",
        "matchedField": "hostname",
        "matchedValue": "camera",
        "source": "fortigate_device_inventory",
        "fetchedAt": "2026-08-29T20:00:00Z",
    }


def test_current_status_fast_path_reads_only_identity_inventory_and_sessions() -> None:
    result = collect_fortigate_current_status(
        "camera",
        api_factory=_factory,
        device_status_reader=lambda ip, _lang: {"state": "online", "ip": ip},
        live_flow_reader=lambda ip: {"sessionCount": 2, "ip": ip},
    )

    assert result["subjectResolution"]["resolvedIp"] == "192.168.16.56"
    assert result["targetStatus"]["state"] == "online"
    assert result["targetFlows"]["sessionCount"] == 2
    assert result["readPlan"] == [
        "target_inventory", "current_sessions",
    ]


def test_auth_window_is_bounded_to_the_managed_device(monkeypatch) -> None:
    queries: list[str] = []

    def query(sql: str) -> list[dict]:
        queries.append(sql)
        return [{"failed_logins": 12, "distinct_sources": 6, "lockouts": 0}]

    monkeypatch.setattr(history, "_q", query)

    result = collect_admin_auth_window(
        "2026-08-31T20:14:00Z",
        "2026-08-31T20:16:00Z",
        managed_device="FG100ETK20014183",
    )

    assert result["available"] is True
    assert result["query_scope"]["managed_device"] == "FG100ETK20014183"
    assert "device_id='FG100ETK20014183'" in queries[0]
    assert "device_name='FG100ETK20014183'" in queries[0]
    assert result["source_role"] == "historical_event_window"
    assert "max_ingest_delay_ms" in result


def test_auth_window_rejects_an_unbounded_device_query() -> None:
    result = collect_admin_auth_window(
        "2026-08-31T20:14:00Z", "2026-08-31T20:16:00Z",
    )

    assert result == {"available": False, "reason": "managed_device_required"}


def test_auth_window_marks_trigger_archive_count_conflict(monkeypatch) -> None:
    event_id = "a" * 64
    monkeypatch.setattr(history, "_q", lambda _sql: [{
        "failed_logins": 5,
        "distinct_sources": 5,
        "lockouts": 0,
        "archived_source_events": 1,
    }])

    result = collect_admin_auth_window(
        "2026-08-31T20:14:00Z",
        "2026-08-31T20:16:00Z",
        managed_device="FG100ETK20014183",
        expected_failure_count=14,
        expected_distinct_sources=5,
        source_event_ids=[event_id],
    )

    assert result["evidence_consistency"]["state"] == "conflict"
    assert result["evidence_consistency"]["trigger_failure_count"] == 14
    assert result["evidence_consistency"]["archive_failure_or_lockout_count"] == 5
