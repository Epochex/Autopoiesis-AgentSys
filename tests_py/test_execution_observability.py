from __future__ import annotations

import json

from core.observability import (
    ExecutionObserver,
    LangfuseTraceExporter,
    NodeObservationEvent,
    ObservationLedger,
    TraceAnalyzer,
)
from core.observability.observer import summarize_value
from domains.network_rca.factory import build_network_rca_service, load_seed_cases


def test_nested_nodes_reconstruct_bottleneck_and_session_trend(tmp_path):
    observer = ExecutionObserver(tmp_path / "observability.jsonl")
    with observer.span(
        trace_id="run-1",
        session_id="session-a",
        case_id="case-a",
        node_name="workflow",
        node_type="workflow",
        input={"query": "diagnose"},
    ):
        with observer.span(
            trace_id="run-1",
            session_id="session-a",
            case_id="case-a",
            node_name="memory.retrieve",
            node_type="retrieval",
        ) as child:
            child.set_result(metrics={"candidate_count": 7})

    trace = TraceAnalyzer(observer.ledger).trace("run-1")

    assert trace.status == "ok"
    assert trace.node_count == 2
    assert trace.bottleneck["node_name"] == "memory.retrieve"
    assert trace.metrics["memory.retrieve.candidate_count"] == 7
    assert trace.nodes[1].parent_span_id == trace.nodes[0].span_id
    session = TraceAnalyzer(observer.ledger).session("session-a")
    assert session["trace_count"] == 1
    assert session["performance_by_node"]["memory.retrieve"]["runs"] == 1


def test_unmatched_start_is_reported_as_incomplete_node(tmp_path):
    ledger = ObservationLedger(tmp_path / "interrupted.jsonl")
    ledger.append(
        NodeObservationEvent(
            trace_id="run-stuck",
            session_id="session-stuck",
            case_id="case-stuck",
            span_id="root",
            node_name="rca.evolving_run",
            node_type="workflow",
            phase="started",
            status="running",
        )
    )

    trace = TraceAnalyzer(ledger).trace("run-stuck")

    assert trace.status == "error"
    assert trace.incomplete_nodes == ["rca.evolving_run"]
    assert trace.finished_at is None


def test_inputs_are_bounded_and_secrets_are_redacted():
    value = summarize_value(
        {
            "api_key": "should-not-appear",
            "nested": {"password": "also-secret"},
            "long": "x" * 700,
            "many": list(range(40)),
        }
    )

    encoded = json.dumps(value)
    assert "should-not-appear" not in encoded
    assert "also-secret" not in encoded
    assert value["api_key"] == "[redacted]"
    assert "truncated" in value["long"]
    assert "truncated" in value["many"][-1]


def test_online_service_emits_every_critical_node_and_groups_runs(tmp_path):
    service = build_network_rca_service(
        tmp_path / "trace.jsonl",
        seed_memory=False,
        start_maintenance=False,
    )
    case = load_seed_cases()[0]

    first, first_report = service.diagnose(case, session_id="incident-42")
    second, second_report = service.diagnose(case, session_id="incident-42")
    trace = TraceAnalyzer(service.observer.ledger).trace(service.last_run_id)

    assert first.root_cause_key == second.root_cause_key == "carrier_down"
    assert first_report.passed and second_report.passed
    names = [node.node_name for node in trace.nodes]
    assert names[0] == "rca.evolving_run"
    for required in (
        "memory.retrieve",
        "memory.evolution.analyze",
        "skills.probe",
        "context.compile",
        "reasoner.diagnose",
        "verifier.verify",
        "memory.consolidate",
        "index.maintenance.trigger",
    ):
        assert required in names
    assert sum(name.startswith("tool.") for name in names) == 2
    retrieval = next(node for node in trace.nodes if node.node_name == "memory.retrieve")
    assert retrieval.metrics["returned_count"] >= 1
    context = next(node for node in trace.nodes if node.node_name == "context.compile")
    assert context.metrics["tokens_after"] > 0
    assert trace.status == "ok"
    session = TraceAnalyzer(service.observer.ledger).session("incident-42")
    assert session["trace_count"] == 2
    service.close()


class _FakeObservation:
    def __init__(self, name, kwargs):
        self.name = name
        self.kwargs = kwargs
        self.children = []
        self.output = None
        self.ended = False

    def start_observation(self, **kwargs):
        child = _FakeObservation(kwargs["name"], kwargs)
        self.children.append(child)
        return child

    def update(self, *, output):
        self.output = output

    def end(self, **_kwargs):
        self.ended = True


class _FakeLangfuseClient:
    def __init__(self):
        self.roots = []

    def start_observation(self, **kwargs):
        root = _FakeObservation(kwargs["name"], kwargs)
        self.roots.append(root)
        return root


def test_langfuse_is_a_downstream_projection_of_local_trace(tmp_path):
    observer = ExecutionObserver(tmp_path / "local.jsonl")
    with observer.span(
        trace_id="run-export",
        session_id="session-export",
        case_id="case-export",
        node_name="workflow",
        node_type="workflow",
    ):
        with observer.span(
            trace_id="run-export",
            session_id="session-export",
            case_id="case-export",
            node_name="reasoner.diagnose",
            node_type="llm",
        ) as generation:
            generation.set_result(output={"answer": "grounded"})

    client = _FakeLangfuseClient()
    LangfuseTraceExporter(client)("run-export", observer.ledger)

    assert len(client.roots) == 1
    assert client.roots[0].name == "workflow"
    assert client.roots[0].children[0].name == "reasoner.diagnose"
    assert client.roots[0].children[0].kwargs["as_type"] == "generation"
    assert client.roots[0].children[0].output == {"answer": "grounded"}
    assert client.roots[0].children[0].ended
