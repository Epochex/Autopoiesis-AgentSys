from __future__ import annotations

from datetime import datetime, timezone

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
    assert repository.get(case.case_id).status == "investigating"


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


def test_pair_gate_requires_same_root_and_earlier_confirmation() -> None:
    control = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 4,
        "candidate_probes": ["a", "b"],
        "unscoped_context_count": 0,
    }
    treatment = {
        "confirmed_roots": ["service_failed"],
        "steps_to_first_confirmation": 1,
        "candidate_probes": ["a", "b"],
        "unscoped_context_count": 0,
        "memory_influenced_order": True,
    }

    assert compare_investigation_pair(control, treatment)["business_value_proven"] is True
    treatment["confirmed_roots"] = ["disk_pressure"]
    assert compare_investigation_pair(control, treatment)["business_value_proven"] is False


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
    repository = InvestigationCaseRepository(tmp_path / "cases.sqlite3")
    now = datetime.now(timezone.utc)
    case = repository.ingest(CaseObservation(
        source=SourceReference("suggestion", "memory-pressure-now"),
        occurred_at=now.isoformat(),
        severity="high",
        subject="collector.service",
        service="memory-health",
        summary="host degradation detected",
    ))
    memory = TieredMemoryStore(enabled=True)
    memory.add(MemoryRecord(
        memory_id="proc-memory-pressure",
        tier="procedural",
        text="verified memory pressure check",
        tags=["root:memory_pressure", "probe:free -m", "fam-perception-selfheal"],
        asset_ids=["collector.service"],
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
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _execution(command, outputs.get(command, "ok")),
    )

    report = investigate.paired_evaluate_case(case.case_id)

    assert report["control"]["confirmed_roots"] == ["memory_pressure"]
    assert report["treatment"]["confirmed_roots"] == ["memory_pressure"]
    assert report["control"]["steps_to_first_confirmation"] > 1
    assert report["treatment"]["steps_to_first_confirmation"] < report["control"]["steps_to_first_confirmation"]
    assert report["acceptance"]["business_value_proven"] is True
