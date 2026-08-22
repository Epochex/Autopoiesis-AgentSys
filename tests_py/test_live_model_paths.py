"""Smoke tests for the model-calling endpoints, because their failures are silent.

Every one of these functions swallows exceptions and substitutes a stub, so a
NameError left behind by a refactor looks exactly like "the model declined" —
which is how a broken cache write went unnoticed after the last cleanup. These
tests call each one with model access switched off, which exercises the whole
body up to the point of the call without spending anything.
"""

from __future__ import annotations

import pytest

from frontend.gateway.app import live, model_access

from hostgate import requires_real_dataset


@pytest.fixture(autouse=True)
def no_paid_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPOIESIS_LLM_ENABLED", "0")
    monkeypatch.setattr(model_access, "CACHE_DIR", tmp_path / "cache")


@requires_real_dataset
def test_every_model_entry_point_is_importable_and_declines_cleanly():
    """A stub answer, not a NameError dressed up as one."""
    for call in (
        lambda: live.assess_device("192.168.1.9", "192.168.1.0/24", {"ip": "192.168.1.9"}),
        lambda: live.assess_mesh("192.168.1.0/24"),
        lambda: live.analyze_graph("192.168.1.0/24"),
        lambda: live.assess_wan("8.8.8.8"),
    ):
        result = call()
        assert isinstance(result, dict)
        assert result.get("ok") is False
        assert "text" in result


@requires_real_dataset
def test_the_switch_is_what_declines_them(monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_LLM_ENABLED", "0")
    assert live.assess_mesh("192.168.1.0/24").get("disabled") is True


def test_posture_synthesis_returns_empty_rather_than_raising():
    assert live._synthesize_posture("192.168.1.0/24", [{"ip": "x", "severity": "high", "verdict": "v"}], "zh") == ""


def test_model_name_is_reachable_from_this_module():
    """The refactor that removed `cfg` left seven orphaned `cfg["model"]` reads."""
    assert isinstance(live.model_name(), str)
