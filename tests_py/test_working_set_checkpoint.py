from __future__ import annotations

from datetime import datetime, timezone

from core.memory.store import MemoryRecord, TieredMemoryStore
from frontend.gateway.app import investigate
from frontend.gateway.app.investigation_session_store import InvestigationSessionStore


class _Execution:
    def __init__(self, command: str, output: str):
        self.command = command
        self.output = output

    def as_evidence(self, evidence_id: str) -> dict:
        return {
            "evidence_id": evidence_id,
            "command": self.command,
            "output": self.output,
            "ok": True,
            "exit_code": 0,
        }


class _Client:
    def __init__(self, *payloads: dict):
        self.payloads = list(payloads)
        self.seen: list[list[dict]] = []

    def complete_json(self, messages, *, schema_name):  # noqa: ANN001, ARG002
        self.seen.append(messages)
        return self.payloads.pop(0)


def _configure_offline(monkeypatch, tmp_path, *, disk_use: int = 30) -> None:
    store = InvestigationSessionStore(tmp_path / "sessions")
    monkeypatch.setattr(investigate, "_session_store", lambda: store)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    monkeypatch.setattr(investigate, "retrieve_ops_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(investigate, "_device_profile_anomaly_types", lambda *_args: [])
    outputs = {
        "hostname": "checkpoint-host\n",
        "uptime": "up 2 days\n",
        "ip -br addr show": "eth0 UP 192.0.2.10/24\n",
        "ip -br link show": "eth0 UP\n",
        "ip route show": "default via 192.0.2.1 dev eth0\n",
        "ip neigh show": "192.0.2.1 dev eth0 REACHABLE\n",
        "systemctl --failed --no-legend": "",
        "df -h": (
            "Filesystem Size Used Avail Use% Mounted on\n"
            f"/dev/sda 100G {disk_use}G 5G {disk_use}% /\n"
        ),
        "free -m": "Mem: 1000 200 100 0 100 700\n",
        "ss -tulpn": "tcp LISTEN 0 128 127.0.0.1:8026 0.0.0.0:*\n",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
        "journalctl -p err -n 40 --no-pager --since -24h": "-- No entries --\n",
        "dmesg -T --level err,crit,alert -x": "",
    }
    monkeypatch.setattr(
        investigate,
        "run",
        lambda command: _Execution(command, outputs.get(command, "ok\n")),
    )
    investigate._SESSIONS.clear()


def _disk_memory() -> TieredMemoryStore:
    observed_at = datetime.now(timezone.utc)
    memory = TieredMemoryStore()
    memory.add(MemoryRecord(
        memory_id="proc-disk-pressure",
        tier="procedural",
        text="disk pressure inspect disk usage first",
        tags=["disk", "pressure", "root:disk_pressure", "probe:df -h"],
        confidence=1.8,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    memory.add(MemoryRecord(
        memory_id="sem-disk-pressure",
        tier="semantic",
        text="recurring disk pressure pattern",
        tags=["disk", "pressure", "root:disk_pressure"],
        confidence=1.4,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    return memory


def test_working_set_is_serializable_and_rebuilt_from_legacy_session(
    monkeypatch, tmp_path
) -> None:
    _configure_offline(monkeypatch, tmp_path)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    opened = investigate.start("检查主机网络", memory_enabled=False)

    checkpoint = opened["working_set"]
    assert checkpoint["goal"] == "检查主机网络"
    assert checkpoint["state_version"] > 0
    assert checkpoint["tool_receipts"]
    assert checkpoint["constraints"]["safe_readonly_probe_only"] is True
    assert opened["memory_value"]["ordering_effect"] == "not_changed"
    assert opened["memory_value"]["memory_hypothesis_result"] == "not_applicable"

    session_id = opened["session_id"]
    legacy = investigate.get(session_id).as_dict()
    legacy.pop("working_set")
    legacy.pop("memory_intervention")
    investigate._session_store().save(legacy)
    investigate._SESSIONS.pop(session_id)

    restored = investigate.get(session_id)
    assert restored.working_set is not None
    assert restored.working_set.transition == "legacy_session_restored"
    assert restored.working_set.tool_receipts
    assert restored.working_set.open_hypotheses


def test_memory_intervention_records_confirmed_real_savings(monkeypatch, tmp_path) -> None:
    _configure_offline(monkeypatch, tmp_path, disk_use=95)
    monkeypatch.setattr(investigate, "_live_memory_store", _disk_memory)

    opened = investigate.start("disk pressure on this host")
    value = opened["memory_value"]

    assert opened["probe_prior"]["no_memory_shadow_order"][0] == "ip -br link show"
    assert opened["probe_prior"]["selected_order"][0] == "df -h"
    assert value["ordering_effect"] == "changed"
    assert value["memory_hypothesis_result"] == "confirmed"
    assert value["executed_order"] == ["df -h"]
    assert value["saved_probe_count"] == len(investigate.TRIAGE_PROBES) - 1
    assert any(
        item["kind"] == "memory_intervention_evaluated"
        for item in opened["trace_events"]
    )


def test_wrong_memory_is_marked_invalidated_and_claims_no_saved_probe(
    monkeypatch, tmp_path
) -> None:
    _configure_offline(monkeypatch, tmp_path, disk_use=30)
    monkeypatch.setattr(investigate, "_live_memory_store", _disk_memory)

    value = investigate.start("disk pressure on this host")["memory_value"]

    assert value["ordering_effect"] == "changed"
    assert value["memory_hypothesis_result"] == "invalidated"
    assert value["invalidated"] is True
    assert value["saved_probe_count"] == 0
    assert "df -h" in value["executed_order"]


def test_follow_up_prompt_uses_working_set_delta(monkeypatch, tmp_path) -> None:
    _configure_offline(monkeypatch, tmp_path)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: None)
    opened = investigate.start("检查主机网络", memory_enabled=False)
    client = _Client(
        {"answer": "第一答", "citations": ["ev-001"], "need_commands": []},
        {"answer": "第二答", "citations": [], "need_commands": []},
    )
    monkeypatch.setattr(investigate, "_client", lambda: client)

    first = investigate.ask(opened["session_id"], "第一问")
    investigate.ask(opened["session_id"], "第二问")
    second_prompt = client.seen[-1][-1]["content"]

    assert first["working_set_delta"]["last_turn"]["question"] == "第一问"
    assert "当前工作集与最近状态增量" in second_prompt
    assert "第一问" in second_prompt
    assert "此前对话" not in second_prompt
