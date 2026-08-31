from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from domains.network_rca.investigation_case import (
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)
from frontend.gateway.app import investigate, investigation_cases, main, remediation
from frontend.gateway.app import investigation_tools
from core.investigate.safe_exec import Execution
from core.eval.investigation_pair import compare_investigation_pair
from core.memory.store import MemoryRecord, TieredMemoryStore


def _execution(command: str, output: str = "ok") -> Execution:
    return Execution(command=command, argv=command.split(), ran=True, output=output, exit_code=0)


def _isolate(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    monkeypatch.setattr(investigate, "_persist_session", lambda *_args: None)


def test_high_severity_case_auto_starts_once_and_targets_managed_gateway(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch)
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    case = repository.ingest(CaseObservation(
        source=SourceReference("alert", "public-deny"),
        occurred_at=datetime.now(timezone.utc).isoformat(),
        severity="high",
        subject="8.8.8.8",
        rule_id="deny-burst",
        summary="public sender caused a denied-flow burst",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {
                "dataClassification": "observed",
                "sourceIp": "8.8.8.8",
                "destinationIp": "203.0.113.10",
                "service": "tcp/5555",
                "action": "deny",
                "trafficSubtype": "local",
                "policyId": 0,
                "policyType": "local-in-policy",
                "sourceInterface": "wan1",
                "sourceInterfaceRole": "wan",
                "denyCount": 200,
                "windowSeconds": 60,
            },
        },
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    called: list[str] = []
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: (called.append(command) or _execution(command)),
    )

    first = investigation_cases.auto_start_pending_cases(repository)
    second = investigation_cases.auto_start_pending_cases(repository)

    assert len(first) == 1 and second == []
    assert first[0]["subject"] == "192.168.1.1"
    assert not any("8.8.8.8" in command for command in called)
    assert first[0]["decision"]["classification"] == "blocked_external_probe"
    assert first[0]["decision"]["state"] == "resolved"
    assert first[0]["retrieval_results"] == []
    assert all(
        event.get("kind") != "memory_candidates_ranked"
        for event in first[0]["trace_events"]
    )
    assert repository.get(case.case_id).status == "resolved"


def test_background_poller_does_not_race_controlled_acceptance_case(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch)
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    case = repository.ingest(CaseObservation(
        source=SourceReference("controlled_fault", "acceptance-1"),
        occurred_at=datetime.now(timezone.utc).isoformat(),
        severity="high",
        subject="controlled.service",
        rule_id="availability_guard_v1",
        summary="controlled service failed",
        payload={
            "dataClassification": "observed",
            "acceptanceRunId": "run-1",
            "incidentFacts": {"dataClassification": "observed"},
        },
    ))

    assert investigation_cases.auto_start_pending_cases(repository) == []

    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _execution(
            command,
            "controlled.service loaded failed failed"
            if command == "systemctl --failed --no-legend" else "ok",
        ),
    )
    started = investigation_cases.auto_start_pending_cases(
        repository, case_ids={case.case_id}
    )
    assert len(started) == 1


def test_delayed_exact_fields_replace_an_incomplete_policy_decision(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch)
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    source = SourceReference("suggestion", "delayed-policy-facts")
    case = repository.ingest(CaseObservation(
        source=source,
        occurred_at=now,
        severity="warning",
        subject="8.8.8.8",
        rule_id="deny-burst",
        service="tcp/5555",
        summary="legacy policy alert",
        payload={"dataClassification": "observed"},
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    monkeypatch.setattr(
        investigation_tools,
        "collect_case_flow_window",
        lambda *_args: {"available": False, "reason": "source_and_destination_ip_required"},
    )
    monkeypatch.setattr(
        investigation_tools,
        "collect_fortigate_context",
        lambda *_args: {"degraded": False, "policies": []},
    )

    first = investigation_cases.auto_start_pending_cases(repository)
    assert first[0]["decision"]["classification"] == "policy_outcome_unresolved"
    assert repository.get(case.case_id).status == "investigating"

    repository.ingest(CaseObservation(
        source=source,
        occurred_at=now,
        severity="warning",
        subject="8.8.8.8",
        rule_id="deny-burst",
        service="tcp/5555",
        summary="exact local-in deny",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {
                "dataClassification": "observed",
                "sourceIp": "8.8.8.8",
                "destinationIp": "192.0.2.10",
                "service": "tcp/5555",
                "action": "deny",
                "trafficSubtype": "local",
                "policyType": "local-in-policy",
                "policyId": 0,
                "sourceInterface": "wan1",
                "sourceInterfaceRole": "wan",
            },
        },
    ))

    refreshed = investigation_cases.auto_start_pending_cases(repository)

    assert refreshed[0]["decision"]["classification"] == "blocked_external_probe"
    assert repository.get(case.case_id).status == "resolved"


def test_confirmed_failed_unit_maps_to_action_and_records_readback(monkeypatch) -> None:
    _isolate(monkeypatch)
    outputs = {
        "systemctl --failed --no-legend": "collector.service loaded failed failed",
        "df -h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /",
        "free -m": "Mem: 1000 100 100 0 0 900",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
    }
    monkeypatch.setattr(investigate, "run", lambda command: _execution(command, outputs.get(command, "ok")))
    monkeypatch.setattr(
        remediation,
        "preflight",
        lambda action, target, *_args: {"eligible": True, "action": action, "target": target},
    )
    opened = investigate.start("采集服务失败", family="fam-perception-selfheal")

    candidate = investigate.action_candidate(opened["session_id"])
    result = investigate.remediate(
        opened["session_id"],
        executor=lambda action, target, **_kwargs: {
            "ran": True,
            "action": action,
            "target": target,
            "outcome": "passed",
            "needs_human": False,
            "execution_id": "exec-1",
        },
    )

    assert candidate["action"] == "restart_unit"
    assert candidate["target"] == "collector.service"
    assert result["case_status"] == "resolved"
    assert result["readback_evidence"]["source"] == "action_readback"


def test_auto_case_executes_only_exact_target_and_closes_from_readback(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch)
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    case = repository.ingest(CaseObservation(
        source=SourceReference("alert", "collector-failed-now"),
        occurred_at=now,
        severity="high",
        subject="collector.service",
        service="service-health",
        rule_id="service-failed",
        summary="collector service entered failed state",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {
                "dataClassification": "observed",
                "observedAt": now,
            },
        },
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    outputs = {
        "systemctl --failed --no-legend": "collector.service loaded failed failed",
        "df -h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /",
        "free -m": "Mem: 1000 100 100 0 0 900",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
    }
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _execution(command, outputs.get(command, "ok")),
    )
    monkeypatch.setattr(
        remediation,
        "preflight",
        lambda action, target, *_args: {
            "eligible": True,
            "action": action,
            "target": target,
            "policy": {"auto_execute": True},
        },
    )
    monkeypatch.setattr(
        remediation,
        "execute",
        lambda action, target, **_kwargs: {
            "ran": True,
            "action": action,
            "target": target,
            "outcome": "passed",
            "needs_human": False,
            "execution_id": "exec-auto-1",
            "at": now,
        },
    )

    result = investigation_cases.auto_start_pending_cases(repository)[0]
    stored = repository.get(case.case_id)

    assert result["decision"]["state"] == "resolved"
    assert result["decision"]["readback"]["outcome"] == "passed"
    assert stored is not None and stored.status == "resolved"
    assert any(item["kind"] == "remediation_started" for item in stored.timeline)
    assert any(item["kind"] == "remediation_completed" for item in stored.timeline)


def test_action_exception_becomes_failed_readback_and_escalation(monkeypatch) -> None:
    _isolate(monkeypatch)
    outputs = {
        "systemctl --failed --no-legend": "collector.service loaded failed failed",
        "df -h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /",
        "free -m": "Mem: 1000 100 100 0 0 900",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
    }
    monkeypatch.setattr(investigate, "run", lambda command: _execution(command, outputs.get(command, "ok")))
    monkeypatch.setattr(
        remediation,
        "preflight",
        lambda action, target, *_args: {"eligible": True, "action": action, "target": target},
    )
    opened = investigate.start(
        "collector failed", family="fam-perception-selfheal", subject="collector.service"
    )

    result = investigate.remediate(
        opened["session_id"],
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport lost")),
    )

    assert result["decision"]["state"] == "escalated"
    assert result["decision"]["readback"]["outcome"] == "failed"
    assert result["readback_evidence"]["ok"] is False


def test_pair_gate_requires_same_root_and_earlier_confirmation() -> None:
    control = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 4,
        "probe_count": 4,
        "candidate_probes": ["a", "b"],
        "decisive_probe_output_fingerprints": {"a": "same"},
        "elapsed_ms": 400,
        "unscoped_context_count": 0,
    }
    treatment = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 1,
        "probe_count": 1,
        "candidate_probes": ["a", "b"],
        "decisive_probe_output_fingerprints": {"a": "same"},
        "elapsed_ms": 100,
        "unscoped_context_count": 0,
        "memory_influenced_order": True,
    }

    assert compare_investigation_pair(control, treatment)["business_value_proven"] is True
    treatment["confirmed_roots"] = ["disk_pressure"]
    assert compare_investigation_pair(control, treatment)["business_value_proven"] is False


def test_pair_compares_decisive_reading_not_unrelated_volatile_probe() -> None:
    control = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 3,
        "probe_count": 5,
        "candidate_probes": ["service", "memory"],
        "decisive_probe_output_fingerprints": {"service": "same"},
        "probe_output_fingerprints": {"service": "same", "memory": "before"},
        "unscoped_context_count": 0,
        "elapsed_ms": 500,
    }
    treatment = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 1,
        "probe_count": 1,
        "candidate_probes": ["service", "memory"],
        "decisive_probe_output_fingerprints": {"service": "same"},
        "probe_output_fingerprints": {"service": "same", "memory": "after"},
        "unscoped_context_count": 0,
        "elapsed_ms": 100,
        "memory_influenced_order": True,
    }

    assert compare_investigation_pair(control, treatment)["business_value_proven"] is True


def test_open_root_can_refresh_router_evidence_through_closed_adapter(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(
        investigation_tools,
        "collect_fortigate_context",
        lambda subject: {"degraded": False, "subject": subject, "policy": "deny"},
    )
    session = investigate.Session(
        session_id="adapter-probe",
        question="policy mismatch",
        family="fam-policy-reachability",
        subject="192.168.1.1",
    )

    item = session.collect("adapter:fortigate_context")

    assert item["ok"] is True
    assert item["source"] == "live_tool"
    assert '"policy": "deny"' in item["output"]


def test_real_pair_runner_proves_earlier_confirmation_without_changing_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOPOIESIS_PAIR_REPETITIONS", "3")
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    now = datetime.now(timezone.utc)
    case = repository.ingest(CaseObservation(
        source=SourceReference("suggestion", "memory-pressure-now"),
        occurred_at=now.isoformat(),
        severity="high",
        subject="host-a",
        service="memory-health",
        summary="host degradation detected",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {"observedAt": now.isoformat()},
        },
    ))
    memory = TieredMemoryStore(enabled=True)
    memory.add(MemoryRecord(
        memory_id="proc-memory-pressure",
        tier="procedural",
        text="verified memory pressure check",
        tags=["root:memory_pressure", "probe:free -m", "fam-perception-selfheal"],
        asset_ids=["host-a"],
        confidence=1.5,
        first_observed_at=now,
        last_observed_at=now,
    ))
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: memory)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    monkeypatch.setattr(investigate, "_persist_session", lambda *_args: None)
    monkeypatch.setattr(investigate, "_pair_log_path", lambda: tmp_path / "pairs.jsonl")
    outputs = {
        "systemctl --failed --no-legend": "",
        "df -h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /",
        "free -m": "Mem: 1000 900 0 0 0 50",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
    }
    def measured_run(command: str) -> Execution:
        time.sleep(0.02)
        return _execution(command, outputs.get(command, "ok"))

    monkeypatch.setattr(investigate, "run", measured_run)

    live = investigate.start(
        "host degradation detected",
        family="fam-perception-selfheal",
        subject="host-a",
        case_id=case.case_id,
    )
    report = investigate.paired_evaluate_case(case.case_id)

    assert report["control"]["confirmed_roots"] == ["memory_pressure"]
    assert report["treatment"]["confirmed_roots"] == ["memory_pressure"]
    assert report["control"]["steps_to_first_confirmation"] > 1
    assert report["treatment"]["steps_to_first_confirmation"] < report["control"]["steps_to_first_confirmation"]
    assert report["acceptance"]["business_value_proven"] is True, report["acceptance"]
    assert report["control"]["session_id"] != live["session_id"]
    assert report["treatment"]["session_id"] != live["session_id"]
    assert report["fixed_script"]["strategy"] == "fixed_script"
    assert report["repetitions_per_strategy"] == 3
    assert all(report["stable_roots_by_strategy"].values())
    measured = repository.get(case.case_id)
    assert measured is not None
    assert any(item["kind"] == "investigation_pair_measured" for item in measured.timeline)


def test_second_real_case_automatically_records_memory_pair(tmp_path, monkeypatch) -> None:
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    memory = TieredMemoryStore(enabled=True)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main, "_investigation_case_repository", repository)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: memory)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    monkeypatch.setattr(investigate, "_persist_session", lambda *_args: None)
    monkeypatch.setattr(investigate, "_pair_log_path", lambda: tmp_path / "pairs.jsonl")
    outputs = {
        "systemctl --failed --no-legend": "",
        "df -h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /",
        "free -m": "Mem: 1000 900 0 0 0 50",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
    }
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _execution(command, outputs.get(command, "ok")),
    )

    first = repository.ingest(CaseObservation(
        source=SourceReference("alert", "memory-first"),
        occurred_at=(now - timedelta(seconds=30)).isoformat(),
        severity="high",
        subject="host-a",
        service="host-health",
        rule_id="resource-alert",
        summary="host degraded",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {"observedAt": (now - timedelta(seconds=30)).isoformat()},
        },
    ))
    first_opened = investigate.start(
        "host degraded", "fam-perception-selfheal", "host-a", first.case_id,
        auto_started=True,
    )
    first_done = investigate.complete(first_opened["session_id"])
    assert first_done["decision"]["classification"] == "memory_pressure"
    repository.append_event(
        first.case_id,
        kind="test_resolution",
        payload={},
        status="resolved",
    )

    # This is a recurrence only if it starts after the first case has been
    # confirmed and its procedure memory is available. Giving both cases the
    # same observedAt would ask the evaluator to use future knowledge.
    recurrence_at = datetime.now(timezone.utc)
    second = repository.ingest(CaseObservation(
        source=SourceReference("alert", "memory-second"),
        occurred_at=recurrence_at.isoformat(),
        severity="high",
        subject="host-a",
        service="host-health",
        rule_id="resource-alert",
        summary="host degraded again",
        payload={
            "dataClassification": "observed",
            "incidentFacts": {"observedAt": recurrence_at.isoformat()},
        },
    ))

    result = investigation_cases.auto_start_pending_cases(repository)[0]
    stored = repository.get(second.case_id)

    assert result["memory_evaluation"]["recurrence"]["same_confirmed_root"] is True
    assert result["memory_evaluation"]["recurrence"]["probe_delta"] > 0
    assert stored is not None
    assert any(item["kind"] == "memory_value_measured" for item in stored.timeline)
