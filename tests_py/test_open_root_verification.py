from __future__ import annotations

from core.investigate.safe_exec import Execution
from frontend.gateway.app import investigate


def _execution(command: str, output: str) -> Execution:
    return Execution(command=command, argv=command.split(), ran=True, output=output, exit_code=0)


def _isolate(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    monkeypatch.setattr(investigate, "_persist_session", lambda *_args: None)


class _Client:
    def __init__(self, first: dict, second: dict) -> None:
        self._payloads = [first, second]

    def complete_json(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return self._payloads.pop(0)


def _payload() -> dict:
    return {
        "diagnosis": "namespace detached",
        "root_cause": "container namespace detached",
        "citations": [],
        "need_commands": [],
        "verification": [
            {"command": "date", "operator": "contains", "value": "UTC"},
            {"command": "uptime", "operator": "contains", "value": "load average"},
        ],
        "runbook": [],
    }


def test_open_root_is_published_only_after_two_frozen_checks(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _execution(
            command,
            "UTC" if command == "date" else ("load average: 0.1" if command == "uptime" else "ok"),
        ),
    )
    monkeypatch.setattr(investigate, "_client", lambda: _Client(_payload(), _payload()))
    opened = investigate.start("容器网络断开", family="fam-does-not-exist")

    result = investigate.analyze(opened["session_id"])

    assert result["root_cause"] == "container namespace detached"
    candidate = next(item for item in result["hypothesis_state"]["hypotheses"] if item["origin"] == "model")
    assert candidate["status"] == "confirmed"
    assert len(candidate["supporting_evidence_ids"]) == 2


def test_one_failed_frozen_check_keeps_the_case_investigating(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(investigate, "run", lambda command: _execution(command, "UTC"))
    monkeypatch.setattr(investigate, "_client", lambda: _Client(_payload(), _payload()))
    opened = investigate.start("容器网络断开", family="fam-does-not-exist")

    result = investigate.analyze(opened["session_id"])

    assert result["root_cause"] == "inconclusive"
    candidate = next(item for item in result["hypothesis_state"]["hypotheses"] if item["origin"] == "model")
    assert candidate["status"] == "rejected"
    assert candidate["opposing_evidence_ids"]
