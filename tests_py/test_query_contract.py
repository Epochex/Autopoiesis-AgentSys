from datetime import datetime, timezone

from domains.network_rca.query_contract import parse_investigation_query


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_current_ip_status_uses_direct_state_only():
    result = parse_investigation_query("192.168.16.56 当前在线状态", now=NOW)

    assert result.subject == "192.168.16.56"
    assert result.intent == "current_status"
    assert result.sources == ("direct_state",)
    assert dict(result.bindings) == {"subject": "ipv4_literal"}


def test_root_cause_with_duration_binds_all_sources_and_window():
    result = parse_investigation_query(
        "过去30分钟 192.168.1.20 为什么反复断开，查根因",
        now=NOW,
    )

    assert result.intent == "root_cause"
    assert result.sources == (
        "direct_state", "event_window", "operational_memory", "official_knowledge",
    )
    assert result.window_start == "2026-09-02T11:30:00+00:00"
    assert result.window_end == "2026-09-02T12:00:00+00:00"


def test_procedure_lookup_skips_live_telemetry():
    result = parse_investigation_query("FortiGate 如何配置 SD-WAN 健康检查", now=NOW)

    assert result.intent == "procedure_lookup"
    assert result.sources == ("official_knowledge", "operational_memory")
