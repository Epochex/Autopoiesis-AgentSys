from __future__ import annotations

import threading
import time

from core.memory.index_maintenance import IndexMaintenanceWorker
from core.memory.store import TieredMemoryStore
from core.observability import ExecutionObserver, NodeObservationEvent, TraceAnalyzer
from core.orchestrator.evolving_service import EvolvingRCAService
from domains.network_rca.factory import build_network_rca_orchestrator, load_seed_cases


def test_error_partial_and_incomplete_nodes_remain_distinguishable(tmp_path):
    observer = ExecutionObserver(tmp_path / "node-statuses.jsonl")
    trace_id = "trace-with-three-failure-modes"

    try:
        with observer.span(
            trace_id=trace_id,
            session_id="session-status",
            case_id="case-status",
            node_name="tool.failed",
            node_type="tool",
        ):
            raise ValueError("probe rejected malformed response")
    except ValueError:
        pass

    with observer.span(
        trace_id=trace_id,
        session_id="session-status",
        case_id="case-status",
        node_name="memory.partial",
        node_type="memory_write",
    ) as partial:
        partial.mark_partial("diagnosis verified but memory commit was skipped")

    observer.ledger.append(
        NodeObservationEvent(
            trace_id=trace_id,
            session_id="session-status",
            case_id="case-status",
            span_id="unfinished-span",
            node_name="index.incomplete",
            node_type="background",
            phase="started",
            status="running",
        )
    )

    trace = TraceAnalyzer(observer.ledger).trace(trace_id)
    nodes = {node.node_name: node for node in trace.nodes}

    assert nodes["tool.failed"].status == "error"
    assert nodes["tool.failed"].finished_at is not None
    assert nodes["tool.failed"].error == "ValueError: probe rejected malformed response"
    assert nodes["memory.partial"].status == "partial"
    assert nodes["memory.partial"].finished_at is not None
    assert nodes["memory.partial"].error == "diagnosis verified but memory commit was skipped"
    assert nodes["index.incomplete"].status == "running"
    assert nodes["index.incomplete"].finished_at is None
    assert trace.incomplete_nodes == ["index.incomplete"]
    assert trace.failed_nodes == ["tool.failed"]
    assert trace.partial_nodes == ["memory.partial"]
    assert trace.status == "error"


def test_long_session_aggregates_many_traces_without_collapsing_run_identity(tmp_path):
    observer = ExecutionObserver(tmp_path / "long-session.jsonl")
    analyzer = TraceAnalyzer(observer.ledger)
    trace_count = 64

    for sequence in range(trace_count):
        trace_id = f"trace-{sequence:03d}"
        with observer.span(
            trace_id=trace_id,
            session_id="long-incident",
            case_id=f"case-{sequence:03d}",
            node_name="workflow",
            node_type="workflow",
        ):
            with observer.span(
                trace_id=trace_id,
                session_id="long-incident",
                case_id=f"case-{sequence:03d}",
                node_name="memory.retrieve",
                node_type="retrieval",
            ) as retrieval:
                retrieval.set_result(metrics={"candidate_count": sequence + 1})

    session = analyzer.session("long-incident")

    assert session["trace_count"] == trace_count
    assert session["failed_traces"] == 0
    assert session["partial_traces"] == 0
    assert session["performance_by_node"]["workflow"]["runs"] == trace_count
    assert session["performance_by_node"]["memory.retrieve"]["runs"] == trace_count
    assert [row["trace_id"] for row in session["traces"]] == [
        f"trace-{sequence:03d}" for sequence in range(trace_count)
    ]
    assert {row["case_id"] for row in session["traces"]} == {
        f"case-{sequence:03d}" for sequence in range(trace_count)
    }
    assert analyzer.trace("trace-063").metrics["memory.retrieve.candidate_count"] == 64


class _BlockingExporter:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self.flush_calls = 0
        self.close_calls = 0

    def __call__(self, trace_id, _ledger):
        self.started.set()
        if not self.release.wait(2.0):
            raise TimeoutError("test exporter was never released")
        self.calls.append(trace_id)

    def flush(self):
        self.flush_calls += 1

    def close(self):
        self.close_calls += 1


def _complete_trace(observer: ExecutionObserver, trace_id: str) -> None:
    with observer.span(
        trace_id=trace_id,
        session_id="export-session",
        case_id=trace_id,
        node_name="workflow",
        node_type="workflow",
    ):
        pass


def test_async_exporter_does_not_block_foreground_and_flush_waits_for_delivery(tmp_path):
    exporter = _BlockingExporter()
    observer = ExecutionObserver(
        tmp_path / "async-export.jsonl",
        exporters=[exporter],
        export_queue_size=4,
    )

    started = time.perf_counter()
    _complete_trace(observer, "async-trace")
    foreground_seconds = time.perf_counter() - started

    assert foreground_seconds < 0.1
    assert exporter.started.wait(1.0)
    assert exporter.calls == []
    exporter.release.set()
    assert observer.flush(timeout=2.0)
    assert exporter.calls == ["async-trace"]
    assert exporter.flush_calls == 1
    assert observer.health()["exports_completed"] == 1
    assert observer.close(timeout=2.0)
    assert exporter.close_calls == 1


def test_full_export_queue_drops_telemetry_without_interrupting_business(tmp_path):
    exporter = _BlockingExporter()
    observer = ExecutionObserver(
        tmp_path / "queue-pressure.jsonl",
        exporters=[exporter],
        export_queue_size=1,
    )
    completed_business_runs: list[str] = []

    _complete_trace(observer, "export-in-progress")
    completed_business_runs.append("export-in-progress")
    assert exporter.started.wait(1.0)
    _complete_trace(observer, "export-queued")
    completed_business_runs.append("export-queued")
    _complete_trace(observer, "export-dropped")
    completed_business_runs.append("export-dropped")

    health = observer.health()
    assert completed_business_runs == [
        "export-in-progress",
        "export-queued",
        "export-dropped",
    ]
    assert health["exports_dropped"] == 1
    assert health["events_written"] == 6
    assert "queue is full" in health["last_error"]
    exporter.release.set()
    assert observer.flush(timeout=2.0)
    assert exporter.calls == ["export-in-progress", "export-queued"]
    assert observer.close(timeout=2.0)


def test_ledger_and_exporter_failures_are_best_effort(monkeypatch, tmp_path):
    observer = ExecutionObserver(tmp_path / "broken-ledger.jsonl")
    business_effects: list[str] = []

    def fail_append(_event):
        raise OSError("disk unavailable")

    monkeypatch.setattr(observer.ledger, "append", fail_append)
    with observer.span(
        trace_id="ledger-failure",
        case_id="case-ledger",
        node_name="business-node",
        node_type="workflow",
    ):
        business_effects.append("completed")

    assert business_effects == ["completed"]
    assert observer.health()["events_dropped"] == 2
    assert "disk unavailable" in observer.health()["last_error"]

    def fail_export(_trace_id, _ledger):
        raise ConnectionError("collector unavailable")

    exporting = ExecutionObserver(
        tmp_path / "broken-exporter.jsonl",
        exporters=[fail_export],
    )
    _complete_trace(exporting, "export-failure")
    assert exporting.flush(timeout=2.0)
    health = exporting.health()
    assert health["export_failures"] == 1
    assert health["exports_completed"] == 1
    assert "collector unavailable" in health["last_error"]
    assert exporting.close(timeout=2.0)


def test_verified_diagnosis_with_failed_consolidation_is_observed_as_partial(tmp_path):
    class _FailingRepository:
        def load_versioned_records(self, *, include_quarantined=True):
            assert include_quarantined
            return []

        def sync_records(self, records, *, expected_versions=None):
            del records, expected_versions
            raise RuntimeError("durable memory commit failed")

    orchestrator = build_network_rca_orchestrator(
        tmp_path / "business-trace.jsonl",
        observability_path=tmp_path / "execution-trace.jsonl",
        seed_memory=False,
    )
    orchestrator.memory = TieredMemoryStore.from_repository(_FailingRepository())
    service = EvolvingRCAService(
        orchestrator,
        maintenance_workers=[],
        raise_on_evolution_error=False,
    )

    diagnosis, verification = service.diagnose(
        load_seed_cases()[0],
        session_id="incident-with-learning-failure",
    )
    trace = TraceAnalyzer(service.observer.ledger).trace(service.last_run_id)
    nodes = {node.node_name: node for node in trace.nodes}
    root = nodes["rca.evolving_run"]

    assert diagnosis.root_cause_key == "carrier_down"
    assert verification.passed
    assert service.last_consolidation is None
    assert service.health()["consolidation_failures"] == 1
    assert trace.status == "error"
    assert root.status == "partial"
    assert root.output["verified"] is True
    assert root.output["memory_committed"] is False
    assert root.metrics["verification_passed"] is True
    assert root.metrics["memory_committed"] is False
    assert nodes["memory.consolidate"].status == "error"
    assert "durable memory commit failed" in nodes["memory.consolidate"].error
    assert trace.failed_nodes == ["memory.consolidate"]
    assert trace.partial_nodes == ["rca.evolving_run"]
    assert service.close()


def test_failed_maintenance_trigger_clears_rolled_back_commit_report(tmp_path):
    class _FailingTriggerWorker:
        def start(self):
            pass

        def trigger(self):
            raise RuntimeError("maintenance scheduler unavailable")

        def stop(self, _timeout=5.0):
            return True

        def stats(self):
            return {"failures": 1}

    orchestrator = build_network_rca_orchestrator(
        tmp_path / "trigger-business.jsonl",
        observability_path=tmp_path / "trigger-nodes.jsonl",
        seed_memory=False,
    )
    service = EvolvingRCAService(
        orchestrator,
        maintenance_workers=[_FailingTriggerWorker()],
        raise_on_evolution_error=False,
    )

    _diagnosis, verification = service.diagnose(
        load_seed_cases()[0],
        session_id="incident-trigger-failure",
    )
    trace = TraceAnalyzer(service.observer.ledger).trace(service.last_run_id)
    nodes = {node.node_name: node for node in trace.nodes}

    assert verification.passed
    assert service.last_consolidation is None
    assert service.health()["consolidation_failures"] == 1
    assert nodes["memory.consolidate"].status == "ok"
    assert nodes["index.maintenance.trigger"].status == "error"
    assert nodes["rca.evolving_run"].status == "partial"
    assert trace.status == "error"
    assert service.close()


def test_background_index_maintenance_failure_is_observed_as_error(tmp_path):
    orchestrator = build_network_rca_orchestrator(
        tmp_path / "maintenance-business.jsonl",
        observability_path=tmp_path / "maintenance-observation.jsonl",
        seed_memory=False,
    )
    service = EvolvingRCAService(
        orchestrator,
        maintenance_workers=[],
        start_maintenance=False,
    )

    def fail_compaction():
        raise RuntimeError("compaction storage failure")

    worker = IndexMaintenanceWorker(
        lambda: True,
        fail_compaction,
        name="failing-index",
        around_run=service._observe_index_maintenance,
    )
    service._maintenance_workers.append(worker)

    assert not worker.run_once(
        context={
            "session_id": "maintenance-session",
            "case_id": "maintenance-case",
            "triggered_by_run_id": "foreground-run",
        }
    )
    traces = TraceAnalyzer(service.observer.ledger).recent(
        session_id="maintenance-session"
    )

    assert len(traces) == 1
    trace = traces[0]
    assert trace.status == "error"
    assert trace.failed_nodes == ["index.maintenance.failing-index"]
    assert trace.partial_nodes == []
    assert trace.incomplete_nodes == []
    node = trace.nodes[0]
    assert node.status == "error"
    assert node.error == "RuntimeError: compaction storage failure"
    assert node.metrics["completed"] is False
    assert node.metrics["failure_delta"] == 1
    assert node.output["worker_after"]["failures"] == 1
    assert node.attributes["triggered_by_run_id"] == "foreground-run"
    assert service.close()
