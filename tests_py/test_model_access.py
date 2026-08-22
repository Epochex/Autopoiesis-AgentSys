"""One door for model calls: the switch, the cache, and the env that used to leak.

Each test here corresponds to a way the old shape cost money silently — a
feature with no off switch, a cache thrown away by every deploy, or a request
that reconfigured the whole process on its way past.
"""

from __future__ import annotations

import os

import pytest

from frontend.gateway.app import model_access


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(model_access, "CACHE_DIR", tmp_path / "llm-cache")
    for key in list(os.environ):
        if key.startswith("AUTOPOIESIS_LLM_"):
            monkeypatch.delenv(key, raising=False)
    return tmp_path


# ── the switch ───────────────────────────────────────────────────────────────


def test_calls_are_enabled_by_default():
    assert model_access.calls_enabled("mesh") is True


def test_the_global_switch_stops_everything(monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_LLM_ENABLED", "0")
    assert model_access.calls_enabled("mesh") is False
    assert model_access.client_for("mesh") is None


def test_a_single_purpose_can_be_turned_off_without_the_rest(monkeypatch):
    """threat_subnet issues one call per device; it deserves its own switch."""
    monkeypatch.setenv("AUTOPOIESIS_LLM_SUBNET", "0")
    assert model_access.calls_enabled("subnet") is False
    assert model_access.calls_enabled("mesh") is True


def test_the_stub_says_which_case_it_is(monkeypatch):
    monkeypatch.setenv("AUTOPOIESIS_LLM_ENABLED", "0")
    off = model_access.unavailable("mesh")
    assert off["disabled"] is True
    monkeypatch.delenv("AUTOPOIESIS_LLM_ENABLED")
    monkeypatch.setattr(model_access, "NOT_CONFIGURED", {"ok": False, "text": "no key"})
    assert model_access.unavailable("mesh").get("disabled") is None


# ── the cache that survives a deploy ─────────────────────────────────────────


def test_a_cached_value_is_returned_without_producing_it_again():
    calls = {"n": 0}

    def produce():
        calls["n"] += 1
        return {"ok": True, "text": "expensive"}

    first = model_access.cached("mesh", "192.168.1.0/24:zh", produce)
    second = model_access.cached("mesh", "192.168.1.0/24:zh", produce)
    assert first == second
    assert calls["n"] == 1, "the second call must not re-pay"


def test_the_cache_is_on_disk_so_a_restart_does_not_re_pay():
    """This is the whole point: module dicts died with the process."""
    model_access.cache_put("mesh", "k", {"ok": True, "v": 1})
    # A fresh import would see the same files; reading straight back proves the
    # value is not held only in memory.
    assert model_access.cache_get("mesh", "k") == {"ok": True, "v": 1}
    assert (model_access.CACHE_DIR / "mesh").exists()


def test_an_expired_entry_is_a_miss():
    model_access.cache_put("mesh", "k", {"ok": True})
    assert model_access.cache_get("mesh", "k", ttl=-1) is None


def test_a_failed_result_is_not_cached():
    """Caching 'key not configured' would keep returning it after the key lands."""
    calls = {"n": 0}

    def produce():
        calls["n"] += 1
        return {"ok": False, "text": "DeepSeek key not configured."}

    model_access.cached("mesh", "k", produce)
    model_access.cached("mesh", "k", produce)
    assert calls["n"] == 2


def test_a_corrupt_cache_file_is_a_miss_not_a_crash():
    path = model_access._path("mesh", "k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert model_access.cache_get("mesh", "k") is None


def test_namespaces_do_not_collide():
    model_access.cache_put("mesh", "same-key", {"which": "mesh"})
    model_access.cache_put("graph", "same-key", {"which": "graph"})
    assert model_access.cache_get("mesh", "same-key") == {"which": "mesh"}
    assert model_access.cache_get("graph", "same-key") == {"which": "graph"}


def test_stats_and_clear_report_real_counts():
    model_access.cache_put("mesh", "a", {"v": 1})
    model_access.cache_put("mesh", "b", {"v": 2})
    model_access.cache_put("graph", "c", {"v": 3})
    stats = model_access.cache_stats()
    assert stats["entries"] == 3
    assert stats["namespaces"]["mesh"]["entries"] == 2
    assert model_access.clear("mesh") == 2
    assert model_access.cache_stats()["entries"] == 1


# ── the environment that used to leak ────────────────────────────────────────


def test_provider_credentials_do_not_outlive_the_request(monkeypatch):
    """One ?provider=deepseek-v4 request used to reconfigure the whole process."""
    from frontend.gateway.app.rca_reader import _llm_env

    monkeypatch.delenv("AUTOPOIESIS_LLM_BASE_URL", raising=False)
    with _llm_env({"AUTOPOIESIS_LLM_BASE_URL": "https://api.deepseek.com/v1"}):
        assert os.environ["AUTOPOIESIS_LLM_BASE_URL"] == "https://api.deepseek.com/v1"
    assert "AUTOPOIESIS_LLM_BASE_URL" not in os.environ


def test_a_pre_existing_value_is_restored_not_deleted(monkeypatch):
    from frontend.gateway.app.rca_reader import _llm_env

    monkeypatch.setenv("AUTOPOIESIS_LLM_MODEL", "original")
    with _llm_env({"AUTOPOIESIS_LLM_MODEL": "temporary"}):
        assert os.environ["AUTOPOIESIS_LLM_MODEL"] == "temporary"
    assert os.environ["AUTOPOIESIS_LLM_MODEL"] == "original"


def test_the_environment_is_restored_even_when_the_body_raises(monkeypatch):
    from frontend.gateway.app.rca_reader import _llm_env

    monkeypatch.delenv("AUTOPOIESIS_LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError):
        with _llm_env({"AUTOPOIESIS_LLM_MODEL": "x"}):
            raise RuntimeError("boom")
    assert "AUTOPOIESIS_LLM_MODEL" not in os.environ


def test_no_overrides_is_a_no_op():
    from frontend.gateway.app.rca_reader import _llm_env

    before = dict(os.environ)
    with _llm_env(None):
        pass
    assert dict(os.environ) == before
