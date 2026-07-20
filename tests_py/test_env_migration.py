from __future__ import annotations

from core.env import autopoiesis_env
from core.llm.provider import OpenAICompatibleClient
from frontend.gateway.app.config import Settings


def test_autopoiesis_env_uses_default_when_both_names_are_absent(monkeypatch):
    monkeypatch.delenv("AUTOPOIESIS_SAMPLE", raising=False)
    monkeypatch.delenv("SELFEVO_SAMPLE", raising=False)
    assert autopoiesis_env("SAMPLE", "fallback") == "fallback"


def test_autopoiesis_env_reads_legacy_name_as_fallback(monkeypatch):
    monkeypatch.delenv("AUTOPOIESIS_SAMPLE", raising=False)
    monkeypatch.setenv("SELFEVO_SAMPLE", "legacy")
    assert autopoiesis_env("SAMPLE") == "legacy"


def test_autopoiesis_env_always_prefers_new_name(monkeypatch):
    monkeypatch.setenv("SELFEVO_SAMPLE", "legacy")
    monkeypatch.setenv("AUTOPOIESIS_SAMPLE", "current")
    assert autopoiesis_env("SAMPLE") == "current"


def test_llm_client_prefers_autopoiesis_configuration(monkeypatch):
    for prefix, value in (("SELFEVO", "legacy"), ("AUTOPOIESIS", "current")):
        monkeypatch.setenv(f"{prefix}_LLM_BASE_URL", f"https://{value}.example/v1")
        monkeypatch.setenv(f"{prefix}_LLM_API_KEY", f"{value}-key")
        monkeypatch.setenv(f"{prefix}_LLM_MODEL", f"{value}-model")

    client = OpenAICompatibleClient()
    assert client.base_url == "https://current.example/v1"
    assert client.api_key == "current-key"
    assert client.model == "current-model"


def test_gateway_settings_accept_legacy_fallback_but_prefer_new(monkeypatch, tmp_path):
    legacy_root = tmp_path / "legacy"
    current_root = tmp_path / "current"
    monkeypatch.setenv("SELFEVO_REPO_ROOT", str(legacy_root))
    monkeypatch.setenv("AUTOPOIESIS_REPO_ROOT", str(current_root))

    settings = Settings.from_env()
    assert settings.repo_root == current_root.resolve()
    assert settings.frontend_dist == (current_root / "frontend" / "dist").resolve()
