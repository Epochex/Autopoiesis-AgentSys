from __future__ import annotations

from core.eval.public_aiops_business import (
    MetricCaseResult,
    _business_value_rows,
    _recurrence_experiment,
    derive_alert_scope,
    normalize_group_asset,
    robust_shift_score,
)


def test_robust_shift_uses_pre_injection_baseline() -> None:
    assert robust_shift_score([1, 1, 2, 2, 2], [2, 8]) == 6.0
    assert robust_shift_score([3, 3, 3, 3], [9, 9]) is None


def test_alert_scope_is_derived_from_firing_alerts() -> None:
    scope = derive_alert_scope(
        {
            "timestamp": "2026-08-31T10:04:00Z",
            "data": {
                "alerts": [
                    {
                        "state": "firing",
                        "activeAt": "2026-08-31T10:00:00Z",
                        "labels": {
                            "alertname": "RequestLatency",
                            "namespace": "otel-demo",
                            "service_name": "checkout",
                        },
                        "annotations": {"description": "checkout latency high"},
                    },
                    {
                        "state": "pending",
                        "labels": {
                            "alertname": "Ignored",
                            "service_name": "other",
                        },
                    },
                ]
            },
        }
    )
    assert scope["firing_alerts"] == 1
    assert scope["asset_ids"] == ["checkout"]
    assert scope["namespaces"] == ["otel-demo"]
    assert scope["fault_domains"] == ["RequestLatency"]
    assert scope["start"] == "2026-08-31T10:00:00Z"
    assert scope["end"] == "2026-08-31T10:04:00Z"


def test_group_asset_normalisation_handles_itbench_aliases() -> None:
    assert normalize_group_asset("frontend-proxy-service-1") == "frontend-proxy"
    assert normalize_group_asset("checkout-pod-2") == "checkout"


def test_recurrence_index_never_needs_root_label_in_query() -> None:
    rows = [
        *[
        MetricCaseResult(
            case_id=f"first-{index}",
            dataset="RE1-OB",
            suite="RE1",
            system="ob",
            root_service="checkoutservice",
            fault="cpu",
            repetition=1,
            service_ranking=("checkoutservice", "frontend"),
            metric_ranking=("checkoutservice_cpu", "frontend_latency-90"),
            root_rank=1,
        ) for index in range(5)],
        MetricCaseResult(
            case_id="repeat",
            dataset="RE1-OB",
            suite="RE1",
            system="ob",
            root_service="checkoutservice",
            fault="cpu",
            repetition=2,
            service_ranking=("frontend", "checkoutservice"),
            metric_ranking=("checkoutservice_cpu", "frontend_latency-90"),
            root_rank=2,
        ),
    ]
    report = _recurrence_experiment(rows)
    assert report["pair_count"] == 1
    assert report["mean_candidate_saving"] == 1
    assert report["harmed_count"] == 0
    assert len(report["pairs"][0]["retrieved_memory_ids"]) == 5
    assert report["pairs"][0]["memory_admitted"] is True


def test_public_report_does_not_invent_live_status() -> None:
    rows = _business_value_rows(
        {"recurrence": {}, "overall": {}},
        {"automatic_scope": {}, "event_retrieval": {}},
        {"task_counts": {}},
        {"automatic_incident_takeover": {"status": "partial"}},
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["automatic_incident_takeover"]["live_site_status"] == "partial"
    assert by_key["open_fault_investigation"]["live_site_status"] == "not_measured"
