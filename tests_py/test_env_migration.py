from __future__ import annotations

import pytest

from core.env import autopoiesis_env
from core.llm.provider import LLMConfigurationError, OpenAICompatibleClient
from frontend.gateway.app.config import Settings
from frontend.gateway.app.providers import _deepseek_cfg


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


def test_llm_client_accepts_shared_deepseek_key(monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.delenv("AUTOPOIESIS_LLM_API_KEY", raising=False)
    monkeypatch.setenv("AUTOPOIESIS_LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shared-key")

    client = OpenAICompatibleClient()

    assert client.api_key == "shared-key"


def test_llm_client_does_not_send_shared_deepseek_key_to_other_endpoints(monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_LLM_BASE_URL", "https://other.example/v1")
    monkeypatch.delenv("AUTOPOIESIS_LLM_API_KEY", raising=False)
    monkeypatch.setenv("AUTOPOIESIS_LLM_MODEL", "other-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shared-key")

    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleClient()


def test_gateway_deepseek_defaults_to_v4_flash(monkeypatch):
    monkeypatch.delenv("DS_V4_BASE_URL", raising=False)
    monkeypatch.delenv("DS_V4_MODEL", raising=False)
    monkeypatch.delenv("DS_V4_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = _deepseek_cfg()

    assert config == {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key": "",
    }


def test_gateway_settings_accept_legacy_fallback_but_prefer_new(monkeypatch, tmp_path):
    legacy_root = tmp_path / "legacy"
    current_root = tmp_path / "current"
    monkeypatch.setenv("SELFEVO_REPO_ROOT", str(legacy_root))
    monkeypatch.setenv("AUTOPOIESIS_REPO_ROOT", str(current_root))

    settings = Settings.from_env()
    assert settings.repo_root == current_root.resolve()
    assert settings.frontend_dist == (current_root / "frontend" / "dist").resolve()


def test_gateway_settings_exposes_optional_knowledge_corpus(monkeypatch, tmp_path):
    corpus = tmp_path / "knowledge.json"
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_CORPUS_PATH", str(corpus))

    settings = Settings.from_env()

    assert settings.knowledge_corpus_path == corpus.resolve()
