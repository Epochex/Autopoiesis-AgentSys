"""Reasoner provider registry for the console.

A provider is either the deterministic rule reasoner or an OpenAI-compatible LLM
endpoint (commercial DeepSeek API or a GPU tunnel). Reachability is probed live
so the frontend switch reflects reality instead of a decorative toggle.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse


def _tcp_open(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _endpoint_reachable(base_url: str) -> bool:
    try:
        u = urlparse(base_url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        return bool(host) and _tcp_open(host, port)
    except Exception:
        return False


def _deepseek_cfg() -> dict:
    return {
        "base_url": os.getenv("DS_V4_BASE_URL", "https://api.deepseek.com/v1"),
        "model": os.getenv("DS_V4_MODEL", "deepseek-chat"),
        "api_key": os.getenv("DS_V4_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or "",
    }


def _gpu_cfg() -> dict:
    return {
        "base_url": os.getenv("SELFEVO_GPU_BASE_URL", "http://127.0.0.1:28000/v1"),
        "model": os.getenv("SELFEVO_GPU_MODEL", "glm-fast"),
        "api_key": os.getenv("SELFEVO_GPU_API_KEY", "sk-local"),
    }


def list_providers() -> list[dict]:
    ds = _deepseek_cfg()
    gpu = _gpu_cfg()
    ds_has_key = bool(ds["api_key"])
    return [
        {
            "id": "rule",
            "label": "Rule baseline",
            "kind": "deterministic",
            "model": "handwritten rules",
            "reachable": True,
            "note": "Deterministic baseline; always available.",
        },
        {
            "id": "deepseek-v4",
            "label": "DeepSeek (API)",
            "kind": "commercial-api",
            "model": ds["model"],
            "reachable": ds_has_key and _endpoint_reachable(ds["base_url"]),
            "note": "Commercial DeepSeek endpoint" + ("" if ds_has_key else " — set DS_V4_API_KEY"),
        },
        {
            "id": "gpu-tunnel",
            "label": "GPU (tunnel)",
            "kind": "self-hosted-gpu",
            "model": gpu["model"],
            "reachable": _endpoint_reachable(gpu["base_url"]),
            "note": "Waseda/Pengcheng GPU over SSH tunnel; open the tunnel first.",
        },
    ]


def resolve_reasoner(provider_id: str) -> tuple[str, dict | None]:
    """Return (reasoner_mode, llm_env). llm_env is None for the rule provider."""
    if provider_id == "rule":
        return "rule", None
    cfg = {"deepseek-v4": _deepseek_cfg, "gpu-tunnel": _gpu_cfg}.get(provider_id)
    if cfg is None:
        return "rule", None
    c = cfg()
    return "llm", {
        "SELFEVO_LLM_BASE_URL": c["base_url"],
        "SELFEVO_LLM_API_KEY": c["api_key"],
        "SELFEVO_LLM_MODEL": c["model"],
    }
