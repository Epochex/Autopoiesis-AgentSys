"""Fail-loud boundaries of the kernel: registry, spec, ledger, compiler, LLM provider.

Deterministic and offline — network transports are monkeypatched fakes.
"""
from __future__ import annotations

import io
import json

import pytest

from core.context.compiler import ContextCompiler
from core.llm import provider as llm_provider
from core.llm.provider import LLMResponseError, OpenAICompatibleClient
from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec
from core.trace.events import TraceEvent
from core.trace.ledger import JSONLTraceLedger


def _spec(name: str = "probe") -> SkillSpec:
    return SkillSpec(name=name, description="probe", tags=["probe"], risk="read_only")


def test_registry_rejects_silent_overwrite_but_allows_explicit_replace():
    registry = SkillRegistry()
    registry.register(_spec(), lambda **kwargs: SkillResult(skill_name="probe"))

    with pytest.raises(ValueError):
        registry.register(_spec(), lambda **kwargs: SkillResult(skill_name="probe"))

    replacement = lambda **kwargs: SkillResult(skill_name="probe", cost=9.0)  # noqa: E731
    registry.register(_spec(), replacement, replace=True)
    assert registry.execute("probe").cost == 9.0


def test_registry_unknown_skill_and_bad_handler_result_fail_loud():
    registry = SkillRegistry()
    registry.register(_spec("bad_contract"), lambda **kwargs: {"not": "a SkillResult"})

    with pytest.raises(KeyError, match="unknown skill"):
        registry.get("never_registered")
    with pytest.raises(TypeError, match="expected SkillResult"):
        registry.execute("bad_contract")


def test_skill_result_evidence_ids_requires_identity():
    good = SkillResult(skill_name="s", evidence=[{"evidence_id": "e1"}, {"evidence_id": "e2"}])
    assert good.evidence_ids() == ["e1", "e2"]

    bad = SkillResult(skill_name="s", evidence=[{"summary": "anonymous evidence"}])
    with pytest.raises(ValueError, match="evidence_id"):
        bad.evidence_ids()


def test_ledger_replay_names_the_corrupt_line(tmp_path):
    path = tmp_path / "trace.jsonl"
    ledger = JSONLTraceLedger(path)
    ledger.append(TraceEvent(run_id="r1", case_id="c1", kind="alert_received", payload={}))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n{not valid json}\n")

    with pytest.raises(ValueError, match=r"trace.jsonl:3"):
        ledger.replay()


def test_ledger_replay_skips_blank_lines_and_preserves_order(tmp_path):
    path = tmp_path / "trace.jsonl"
    ledger = JSONLTraceLedger(path)
    ledger.append(TraceEvent(run_id="r1", case_id="c1", kind="alert_received", payload={}))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    ledger.append(TraceEvent(run_id="r1", case_id="c1", kind="diagnosis_completed", payload={}))

    assert [event.kind for event in ledger.replay()] == ["alert_received", "diagnosis_completed"]


def test_context_compiler_requires_evidence_identity_but_tolerates_missing_summary():
    compiler = ContextCompiler(token_budget=100)

    with pytest.raises(ValueError, match="evidence_id"):
        compiler.compile(
            case_id="c1",
            query="q",
            memories_by_tier={},
            current_evidence=[{"summary": "no id"}],
            required_evidence=[],
        )

    # induced-skill-shaped evidence (no source/summary) must still compile
    packet = compiler.compile(
        case_id="c1",
        query="q",
        memories_by_tier={},
        current_evidence=[{"evidence_id": "ev-induced", "kind": "induced_skill_match"}],
        required_evidence=[],
    )
    assert packet.included_evidence_ids == ["ev-induced"]


def test_context_compiler_rejects_nonpositive_budget():
    with pytest.raises(ValueError):
        ContextCompiler(token_budget=0)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(base_url="http://fake.local/v1", api_key="k", model="m")


def test_llm_client_rejects_malformed_envelope_and_non_json_content(monkeypatch):
    client = _client()

    monkeypatch.setattr(llm_provider.request, "urlopen", lambda req, timeout: _FakeResponse(b'{"no_choices": true}'))
    with pytest.raises(LLMResponseError, match="malformed completion envelope"):
        client.complete_json([{"role": "user", "content": "hi"}], schema_name="diag")

    envelope = json.dumps({"choices": [{"message": {"content": "not json at all"}}]}).encode("utf-8")
    monkeypatch.setattr(llm_provider.request, "urlopen", lambda req, timeout: _FakeResponse(envelope))
    with pytest.raises(LLMResponseError, match="not valid JSON"):
        client.complete_json([{"role": "user", "content": "hi"}], schema_name="diag")


def test_llm_client_returns_parsed_object_and_rejects_empty_messages(monkeypatch):
    client = _client()
    envelope = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode("utf-8")
    monkeypatch.setattr(llm_provider.request, "urlopen", lambda req, timeout: _FakeResponse(envelope))

    assert client.complete_json([{"role": "user", "content": "hi"}], schema_name="diag") == {"ok": True}
    with pytest.raises(ValueError):
        client.complete_json([], schema_name="diag")
