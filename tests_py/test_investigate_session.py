"""What the session layer must not let the model get away with.

The interesting cases are all about who decides: the model proposes a runbook,
but the risk class is re-derived here; the model cites evidence, but only ids
the session actually holds survive; the model asks for more commands, but the
allowlist decides which of them run.
"""

from __future__ import annotations

import pytest

from frontend.gateway.app import investigate


class FakeClient:
    """Stands in for the LLM. Returns whatever the test wants it to say."""

    def __init__(self, *payloads: dict):
        self.payloads = list(payloads)
        self.seen: list[list[dict]] = []

    def complete_json(self, messages, *, schema_name):  # noqa: ANN001
        self.seen.append(messages)
        return self.payloads.pop(0) if self.payloads else {}


@pytest.fixture()
def session_id():
    opened = investigate.start("eth2 现在什么状态", family="fam-host-config-drift")
    return opened["session_id"]


def _use(monkeypatch, client):
    monkeypatch.setattr(investigate, "_client", lambda: client)


# ── evidence is real before anything reasons ─────────────────────────────────


def test_start_runs_probes_and_files_their_real_output(session_id):
    session = investigate.get(session_id)
    assert session.evidence, "a session must open with readings, not an empty context"
    assert all("evidence_id" in item for item in session.evidence)
    hostname = next(i for i in session.evidence if i["command"] == "hostname")
    assert hostname["ok"] is True and hostname["output"]


def test_family_probes_are_added_to_the_baseline(session_id):
    commands = {item["command"] for item in investigate.get(session_id).evidence}
    assert "ip -br link show" in commands
    assert "cloud-init status --long" in commands


def test_unknown_family_still_gets_the_baseline():
    opened = investigate.start("怎么了", family="fam-does-not-exist")
    assert len(opened["evidence"]) >= len(investigate.BASELINE_PROBES)


def test_subject_probes_are_only_added_when_the_subject_is_safe():
    opened = investigate.start("查一下", family=None, subject="192.168.1.23")
    commands = {item["command"] for item in opened["evidence"]}
    assert any("192.168.1.23" in command for command in commands)

    hostile = investigate.start("查一下", family=None, subject="x; rm -rf /")
    commands = {item["command"] for item in hostile["evidence"]}
    assert not any("rm -rf" in command for command in commands)


# ── the model proposes, this layer decides ───────────────────────────────────


def test_a_writing_step_the_model_labelled_readonly_is_forced_to_gated(monkeypatch, session_id):
    """The label that earns a Run button is re-derived, never taken on trust."""
    _use(monkeypatch, FakeClient({
        "diagnosis": "见 [ev-001]",
        "citations": ["ev-001"],
        "runbook": [
            {"n": 1, "risk": "readonly", "what": "重启采集器", "command": "systemctl restart netops-collector", "why": "x"},
        ],
    }))
    result = investigate.analyze(session_id)
    step = result["runbook"][0]
    assert step["risk"] == "gated"
    assert step["runnable"] is False


def test_a_genuinely_readonly_step_stays_runnable(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": [{"n": 1, "risk": "readonly", "what": "看链路", "command": "ip -br link show eth2", "why": "x"}],
    }))
    step = investigate.analyze(session_id)["runbook"][0]
    assert step["risk"] == "readonly"
    assert step["runnable"] is True


def test_model_marking_a_step_gated_is_respected_even_if_the_command_reads(monkeypatch, session_id):
    """Downgrading risk is allowed to the model; upgrading it is not."""
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": [{"n": 1, "risk": "gated", "what": "小心", "command": "ip -br link show", "why": "x"}],
    }))
    assert investigate.analyze(session_id)["runbook"][0]["risk"] == "gated"


def test_malformed_runbook_entries_are_dropped_not_crashed_on(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": ["not a dict", {"command": "uptime", "what": "看负载"}, None],
    }))
    runbook = investigate.analyze(session_id)["runbook"]
    assert len(runbook) == 1
    assert runbook[0]["command"] == "uptime"


# ── citations must exist ─────────────────────────────────────────────────────


def test_invented_citations_are_dropped(monkeypatch, session_id):
    """A hallucinated reading has no id to point at. That is the whole check."""
    _use(monkeypatch, FakeClient({
        "diagnosis": "根据 [ev-999] 判断",
        "citations": ["ev-001", "ev-999", "ev-abc"],
        "runbook": [],
    }))
    citations = investigate.analyze(session_id)["citations"]
    assert "ev-001" in citations
    assert "ev-999" not in citations
    assert "ev-abc" not in citations


# ── running steps ────────────────────────────────────────────────────────────


def test_gated_step_has_no_executor(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": [{"n": 1, "risk": "gated", "what": "改策略", "command": "config firewall policy", "why": "x"}],
    }))
    investigate.analyze(session_id)
    outcome = investigate.run_step(session_id, 1)
    assert outcome["ran"] is False
    assert outcome["refused"] is True
    assert "由人执行" in outcome["reason"]


def test_readonly_step_runs_and_becomes_evidence(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": [{"n": 1, "risk": "readonly", "what": "看时间", "command": "date", "why": "x"}],
    }))
    investigate.analyze(session_id)
    before = len(investigate.get(session_id).evidence)
    outcome = investigate.run_step(session_id, 1)
    assert outcome["ran"] is True
    assert outcome["output"]
    assert len(investigate.get(session_id).evidence) == before + 1


def test_run_all_stops_at_the_first_step_it_may_not_run(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({
        "diagnosis": "d", "citations": [],
        "runbook": [
            {"n": 1, "risk": "readonly", "what": "a", "command": "uptime", "why": "x"},
            {"n": 2, "risk": "gated", "what": "b", "command": "systemctl restart x", "why": "x"},
            {"n": 3, "risk": "readonly", "what": "c", "command": "date", "why": "x"},
        ],
    }))
    investigate.analyze(session_id)
    outcome = investigate.run_all(session_id)
    assert outcome["stopped_at"] == 2
    assert [item["step"] for item in outcome["results"]] == [1, 2]
    assert all(item["step"] != 3 for item in outcome["results"]), "must not run past the gate"


def test_unknown_step_is_reported_not_ignored(session_id):
    outcome = investigate.run_step(session_id, 99)
    assert outcome["refused"] is True


# ── multi-turn memory ────────────────────────────────────────────────────────


def test_follow_up_sees_every_earlier_reading(monkeypatch, session_id):
    client = FakeClient({"answer": "a1", "citations": [], "need_commands": []},
                        {"answer": "a2", "citations": [], "need_commands": []})
    _use(monkeypatch, client)
    investigate.ask(session_id, "第一问")
    investigate.ask(session_id, "第二问")
    second_prompt = client.seen[-1][-1]["content"]
    assert "第一问" in second_prompt, "prior turns must stay in view"
    assert "[ev-001]" in second_prompt, "prior evidence must stay in view"


def test_follow_up_can_request_more_readings_and_they_are_filed(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({"answer": "需要再看", "citations": [], "need_commands": ["date"]}))
    before = len(investigate.get(session_id).evidence)
    result = investigate.ask(session_id, "再查查")
    assert len(result["evidence"]) == 1
    assert len(investigate.get(session_id).evidence) == before + 1


def test_a_requested_command_outside_the_allowlist_is_refused_not_run(monkeypatch, session_id):
    _use(monkeypatch, FakeClient({"answer": "x", "citations": [], "need_commands": ["rm -rf /"]}))
    result = investigate.ask(session_id, "清一下")
    assert result["evidence"][0]["ok"] is False
    assert result["evidence"][0]["refused"]


def test_turns_are_capped(monkeypatch, session_id):
    session = investigate.get(session_id)
    session.turns = [{"question": "q", "answer": "a"}] * investigate.MAX_TURNS
    _use(monkeypatch, FakeClient({"answer": "should not be reached"}))
    assert "上限" in investigate.ask(session_id, "再问")["answer"]


# ── degraded mode ────────────────────────────────────────────────────────────


def test_without_a_model_the_evidence_still_stands(monkeypatch, session_id):
    _use(monkeypatch, None)
    result = investigate.analyze(session_id)
    assert result["degraded"] is True
    assert result["runbook"] == []
    assert investigate.get(session_id).evidence, "probes ran regardless of the model"


def test_unknown_session_is_a_clear_error():
    with pytest.raises(KeyError):
        investigate.get("nope")
