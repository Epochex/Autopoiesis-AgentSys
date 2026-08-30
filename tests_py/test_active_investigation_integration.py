from __future__ import annotations

from core.investigate.safe_exec import Execution
from core.memory.operational_repository import InMemoryOperationalRepository
from frontend.gateway.app import investigate, main
from frontend.gateway.app.operational_memory import OperationalMemoryService


def _execution(command: str, output: str = "ok", *, ok: bool = True) -> Execution:
    return Execution(
        command=command,
        argv=command.split(),
        ran=True,
        output=output,
        exit_code=0 if ok else 1,
    )


def _isolate(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    monkeypatch.setattr(investigate, "_persist_session", lambda *_args: None)


def test_open_question_executes_an_active_slice_and_keeps_the_tail(monkeypatch) -> None:
    _isolate(monkeypatch)
    called: list[str] = []

    def execute(command: str) -> Execution:
        called.append(command)
        outputs = {
            "ip -br link show": "eth2 UP",
            "ip route show": "default via 192.168.1.1 dev eth2",
            "ip neigh show": "192.168.1.1 dev eth2 REACHABLE",
            "systemctl --failed --no-legend": "",
        }
        return _execution(command, outputs.get(command, "ok"))

    monkeypatch.setattr(investigate, "run", execute)
    opened = investigate.start("这个网络现在有什么问题")

    assert len(opened["probe_rounds"]) == investigate.OPENING_ACTIVE_PROBE_BUDGET
    assert len(opened["hypothesis_state"]["hypotheses"]) == len(investigate._ACTIVE_ROOTS) - 1
    assert called == [
        *investigate.BASELINE_PROBES,
        *[item["command"] for item in opened["probe_rounds"]],
    ]
    available = {
        item["description"]
        for item in opened["hypothesis_state"]["probes"]
        if item["status"] == "available"
    }
    attempted = {item["command"] for item in opened["probe_rounds"]}
    expected_tail = {
        spec.probe
        for root_id, spec in investigate._ACTIVE_ROOTS.items()
        if root_id != "neighbor_unreachable" and spec.probe not in attempted
    }
    assert available == expected_tail


def test_query_terms_move_the_relevant_probe_ahead_of_the_catalogue(monkeypatch) -> None:
    _isolate(monkeypatch)
    called: list[str] = []

    def execute(command: str) -> Execution:
        called.append(command)
        if command == "df -h":
            return _execution(command, "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 9.5G .5G 95% /")
        return _execution(command, "ok")

    monkeypatch.setattr(investigate, "run", execute)
    opened = investigate.start("disk pressure filesystem is full")

    assert opened["probe_rounds"][0]["command"] == "df -h"
    disk = next(
        item for item in opened["hypothesis_state"]["hypotheses"]
        if item["hypothesis_id"] == "disk_pressure"
    )
    assert disk["status"] == "confirmed"
    assert disk["supporting_evidence_ids"]


def test_cross_entity_history_is_removed_from_model_context(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(investigate, "run", lambda command: _execution(command))
    session = investigate.Session(
        session_id="retrieval-scope",
        question="192.168.1.27 为什么断流",
        family=None,
        subject="192.168.1.27",
    )
    session.retrieval_results = [
        {
            "kind": "incident_dossier",
            "item_id": "same-device",
            "summary": "192.168.1.27 link loss",
            "source": "operational_memory",
            "matched_on": ["subject"],
        },
        {
            "kind": "incident_dossier",
            "item_id": "other-device",
            "summary": "192.168.1.99 link loss",
            "source": "operational_memory",
            "matched_on": ["query_terms"],
        },
        {
            "kind": "knowledge_document",
            "item_id": "link-manual",
            "summary": "192.168.1.27 断流时如何检查 link state",
            "source": "operations_knowledge_base",
            "matched_on": ["query_terms"],
        },
    ]

    investigate._constrain_retrieval_results(session)

    by_id = {item["item_id"]: item for item in session.retrieval_results}
    assert by_id["same-device"]["selected_for_context"] is True
    assert by_id["other-device"]["selected_for_context"] is False
    assert "entity_unreachable" in by_id["other-device"]["drop_reasons"]
    assert by_id["link-manual"]["selected_for_context"] is True


def test_settled_unique_root_writes_memory_without_operator_evidence(monkeypatch) -> None:
    _isolate(monkeypatch)
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)
    monkeypatch.setattr(main, "_operational_memory", service)
    monkeypatch.setattr(investigate, "_client", lambda: None)

    def execute(command: str) -> Execution:
        if command == "ip -br link show":
            return _execution(command, "eth9 DOWN")
        if command.startswith("ip route show"):
            return _execution(command, "default via 192.168.1.1 dev eth2")
        return _execution(command, "ok")

    monkeypatch.setattr(investigate, "run", execute)
    opened = investigate.start(
        "本机物理链路是否中断",
        family="fam-host-config-drift",
    )
    result = investigate.analyze(opened["session_id"])

    assert result["memory_commit"]["committed"] is True
    dossier = service.dossiers.get(f"investigate:{opened['session_id']}")
    assert dossier is not None
    assert dossier.root_causes[0].origin == "analysis"
    assert dossier.root_causes[0].status == "confirmed"
    assert all(item.source_type != "operator" for item in dossier.evidence)


def test_failed_probe_keeps_candidate_open_and_blocks_memory_write(monkeypatch) -> None:
    _isolate(monkeypatch)
    service = OperationalMemoryService(InMemoryOperationalRepository(), durable=False)
    monkeypatch.setattr(main, "_operational_memory", service)
    monkeypatch.setattr(investigate, "_client", lambda: None)

    def execute(command: str) -> Execution:
        if command.startswith("ip route show"):
            return _execution(command, "permission denied", ok=False)
        if command == "ip -br link show":
            return _execution(command, "eth2 UP")
        return _execution(command, "ok")

    monkeypatch.setattr(investigate, "run", execute)
    opened = investigate.start("检查链路", family="fam-host-config-drift")
    result = investigate.analyze(opened["session_id"])

    route = next(
        item for item in result["hypothesis_state"]["hypotheses"]
        if item["hypothesis_id"] == "default_route_missing"
    )
    assert route["status"] == "testing"
    assert result["memory_commit"] == {
        "committed": False,
        "reason": "competing_hypotheses_unresolved",
    }
    assert service.dossiers.get(f"investigate:{opened['session_id']}") is None


def test_open_ended_model_root_is_persisted_as_unconfirmed_candidate(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(investigate, "run", lambda command: _execution(command, "LISTEN"))

    class Client:
        payloads = [
            {
                "diagnosis": "需要检查网络命名空间",
                "root_cause": "container network namespace was detached",
                "citations": ["ev-001"],
                "need_commands": ["ss -tulpn"],
                "runbook": [],
            },
            {
                "diagnosis": "仍需结构化验证",
                "root_cause": "container network namespace was detached",
                "citations": ["ev-001"],
                "need_commands": [],
                "runbook": [],
            },
        ]

        def complete_json(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return self.payloads.pop(0)

    monkeypatch.setattr(investigate, "_client", Client)
    opened = investigate.start("容器服务为什么断网", family="fam-does-not-exist")
    result = investigate.analyze(opened["session_id"])

    candidate = next(
        item for item in result["hypothesis_state"]["hypotheses"]
        if item["hypothesis_id"].startswith("model:")
    )
    assert candidate["statement"] == "container network namespace was detached"
    assert candidate["status"] == "testing"
    assert candidate["supporting_evidence_ids"] == []
    assert result["memory_commit"]["committed"] is False
