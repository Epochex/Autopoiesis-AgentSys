"""Accounting and context budgeting.

The context tests matter more than the arithmetic ones: a session that resends
every reading in full on every turn costs quadratically more than one that does
not, for no extra insight, and that is how a day's budget disappears.
"""

from __future__ import annotations

import json

import pytest

from core.llm import cost
from frontend.gateway.app import investigate


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "cost.jsonl"
    monkeypatch.setattr(cost, "LEDGER_PATH", path)
    return path


# ── accounting ───────────────────────────────────────────────────────────────


def test_usage_is_recorded_with_a_cost(ledger):
    cost.record("deepseek-v4-pro", "rca_analysis", {"prompt_tokens": 10_000, "completion_tokens": 1_000}, "s1")
    row = json.loads(ledger.read_text().strip())
    assert row["total_tokens"] == 11_000
    assert row["cost_cny"] > 0
    assert row["purpose"] == "rca_analysis"
    assert row["session_id"] == "s1"


def test_a_call_without_usage_is_still_recorded(ledger):
    """A silent call must not vanish from the ledger; zero tokens is a signal."""
    cost.record("deepseek-v4-pro", "rca_followup", None)
    row = json.loads(ledger.read_text().strip())
    assert row["total_tokens"] == 0
    assert row["usage_reported"] is False


def test_output_tokens_cost_more_than_input(ledger):
    heavy_in = cost.Usage("deepseek-v4-pro", "x", 100_000, 0)
    heavy_out = cost.Usage("deepseek-v4-pro", "x", 0, 100_000)
    assert heavy_out.cost_cny() > heavy_in.cost_cny()


def test_unknown_model_still_gets_a_cost(ledger):
    assert cost.Usage("some-new-model", "x", 1_000, 1_000).cost_cny() > 0


def test_summary_breaks_spend_down_by_purpose(ledger):
    cost.record("deepseek-v4-pro", "rca_analysis", {"prompt_tokens": 50_000, "completion_tokens": 2_000})
    cost.record("deepseek-v4-pro", "rca_followup", {"prompt_tokens": 5_000, "completion_tokens": 500})
    out = cost.summary(hours=24)
    assert out["calls"] == 2
    assert set(out["by_purpose"]) == {"rca_analysis", "rca_followup"}
    # Ordered most expensive first, so the thing to fix is at the top.
    assert list(out["by_purpose"])[0] == "rca_analysis"
    assert out["largest_call"]["purpose"] == "rca_analysis"


def test_summary_of_an_empty_ledger_is_zero_not_an_error(ledger):
    out = cost.summary()
    assert out["calls"] == 0 and out["total_cost_cny"] == 0
    assert out["average_tokens_per_call"] == 0


def test_a_broken_ledger_line_does_not_sink_the_summary(ledger):
    ledger.write_text('{"at":"2026-08-22T01:00:00+00:00","total_tokens":5,"cost_cny":0.1}\nnot json\n')
    assert cost.summary(hours=100_000)["calls"] == 1


# ── the per-call ceiling ─────────────────────────────────────────────────────


def test_an_oversized_prompt_is_refused_before_it_is_sent():
    """Sending it and then regretting it still costs the input tokens."""
    from core.llm.provider import LLMBudgetError, OpenAICompatibleClient

    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com/v1", api_key="k", model="m", max_prompt_chars=1_000
    )
    with pytest.raises(LLMBudgetError, match="trim the context"):
        client.complete_json([{"role": "user", "content": "x" * 5_000}], schema_name="s")


# ── context budgeting ────────────────────────────────────────────────────────


def _session_with(monkeypatch, count: int, size: int):
    opened = investigate.start("q", family=None)
    session = investigate.get(opened["session_id"])
    session.evidence = [
        {"evidence_id": f"ev-{i:03d}", "command": f"cmd{i}", "output": "y" * size, "ok": True}
        for i in range(1, count + 1)
    ]
    return session


def test_a_reading_is_excerpted_head_and_tail_not_just_truncated(monkeypatch):
    session = _session_with(monkeypatch, 1, 20_000)
    session.evidence[0]["output"] = "START" + "y" * 19_000 + "ERROR AT THE END"
    block = investigate._evidence_block(session)
    assert "START" in block
    assert "ERROR AT THE END" in block, "the tail is where errors are; it must survive"
    assert "略去" in block


def test_the_whole_block_stays_under_the_ceiling(monkeypatch):
    session = _session_with(monkeypatch, 200, 20_000)
    block = investigate._evidence_block(session)
    assert len(block) <= investigate.MAX_CONTEXT_CHARS + 500


def test_when_the_ceiling_bites_the_oldest_readings_drop_and_it_says_so(monkeypatch):
    session = _session_with(monkeypatch, 200, 5_000)
    block = investigate._evidence_block(session)
    assert "ev-200" in block, "the newest reading must survive"
    assert "未纳入本次上下文" in block, "dropping evidence silently would be worse than dropping it"


def test_follow_up_sends_only_recent_readings_in_full(monkeypatch):
    session = _session_with(monkeypatch, 30, 4_000)
    recent = {f"ev-{i:03d}" for i in range(25, 31)}
    full = investigate._evidence_block(session)
    trimmed = investigate._evidence_block(session, full_ids=recent)
    assert len(trimmed) < len(full), "a follow-up must not re-pay for every reading"


def test_a_cited_reading_stays_in_full_even_when_it_is_old(monkeypatch):
    session = _session_with(monkeypatch, 12, 3_000)
    session.evidence[0]["output"] = "OLD BUT LOAD BEARING " + "z" * 3_000
    block = investigate._evidence_block(session, full_ids={"ev-001"})
    detail = block.split("[ev-001]")[1][:1200]
    assert "OLD BUT LOAD BEARING" in detail
    assert len(detail) > investigate.DIGEST_CHARS


def test_a_cited_reading_survives_the_ceiling_even_when_much_older(monkeypatch):
    """Age must not outrank relevance: the conversation is built on cited readings."""
    session = _session_with(monkeypatch, 40, 3_000)
    block = investigate._evidence_block(session, full_ids={"ev-001"})
    assert "[ev-001]" in block, "a cited reading must not be dropped for being old"
    assert "未纳入本次上下文" in block, "and the drop must still be declared"


def test_prewarm_is_off_unless_explicitly_enabled(monkeypatch):
    """It costs a paid call per subnet per language on every process start."""
    from frontend.gateway.app import main as gateway

    started: list[str] = []
    monkeypatch.setattr("threading.Thread", lambda **kw: type("T", (), {"start": lambda _s: started.append("ran")})())

    monkeypatch.delenv("AUTOPOIESIS_PREWARM", raising=False)
    gateway._start_prewarm()
    assert started == [], "prewarm must not fire by default"

    monkeypatch.setenv("AUTOPOIESIS_PREWARM", "1")
    gateway._start_prewarm()
    assert started == ["ran"]


def test_test_runs_do_not_write_into_the_production_ledger(monkeypatch):
    from core.llm import cost as cost_module

    monkeypatch.delenv("AUTOPOIESIS_COST_LEDGER", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert "autopoiesis-runtime" not in str(cost_module._default_ledger())
