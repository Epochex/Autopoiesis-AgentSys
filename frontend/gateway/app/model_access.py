"""One door for every model call the gateway makes, and one cache behind it.

Before this, four endpoints each carried their own copy of the same six lines —
read the provider config, check for a key, build a client, return a
"not configured" stub — and each kept its own in-process dict as a cache. Two
consequences followed from that shape:

**There was no single place that could say no.** The provider selector on the
console governed one endpoint; the rest called the paid model whenever a key
existed, so turning spend off meant removing the key.

**Every restart re-paid for everything.** The caches were module-level dicts,
so a deploy — which restarts the service — threw away work that had already
been bought and bought it again. On a day with twelve restarts that is the
whole bill.

Both are fixed here rather than in four places. Callers ask for a client
through :func:`client_for`, which honours a global switch and a per-purpose
one, and wrap expensive work in :func:`cached`, which is backed by disk and so
survives the next deploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

CACHE_DIR = Path(os.getenv("AUTOPOIESIS_LLM_CACHE_DIR", "/data/autopoiesis-production/llm-cache"))

# Model-derived views of slow-moving facts — a subnet's device profiles, a
# topology's relationships — do not need re-deriving every day, and re-deriving
# them is what the bill is made of.
DEFAULT_TTL_SEC = float(os.getenv("AUTOPOIESIS_LLM_CACHE_TTL", 7 * 24 * 3600))

_LOCK = threading.Lock()

NOT_CONFIGURED = {"ok": False, "text": "DeepSeek key not configured."}
DISABLED = {
    "ok": False,
    "text": "模型调用已关闭（AUTOPOIESIS_LLM_ENABLED=0）。只读检查与规则推理不受影响。",
    "disabled": True,
}


def calls_enabled(purpose: str = "") -> bool:
    """Whether a paid call may be made at all.

    Two switches, because they answer different questions. The global one is
    for "stop spending now"; the per-purpose one is for "this particular
    feature is not worth what it costs" — ``threat_subnet`` issues one call per
    device in a subnet, which is a different order of expense from the rest.
    """
    if os.getenv("AUTOPOIESIS_LLM_ENABLED", "1") == "0":
        return False
    if purpose:
        flag = os.getenv(f"AUTOPOIESIS_LLM_{purpose.upper()}", "")
        if flag == "0":
            return False
    return True


def client_for(purpose: str, *, timeout_sec: int = 60):
    """A configured client, or None with the reason available from the caller.

    Returning None rather than raising keeps the call sites shaped the way they
    already were: they substitute a stub response and the page still renders.
    """
    if not calls_enabled(purpose):
        return None

    from core.llm.provider import LLMConfigurationError, OpenAICompatibleClient

    from . import providers

    cfg = providers._deepseek_cfg()
    if not cfg.get("api_key"):
        return None
    try:
        return OpenAICompatibleClient(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout_sec=timeout_sec,
            usage_tag=purpose,
        )
    except LLMConfigurationError:
        return None


def model_name() -> str:
    """The model a call would use, for stamping into a result payload."""
    from . import providers

    return providers._deepseek_cfg().get("model") or "unknown"


def unavailable(purpose: str) -> dict[str, Any]:
    """The stub a caller returns when no call may be made, saying which case."""
    return dict(DISABLED) if not calls_enabled(purpose) else dict(NOT_CONFIGURED)


def _path(namespace: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / namespace / f"{digest}.json"


def cache_get(namespace: str, key: str, ttl: float = DEFAULT_TTL_SEC) -> Any | None:
    """Read a cached value, or None when absent, expired or unreadable."""
    path = _path(namespace, key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(raw.get("at", 0)) > ttl:
        return None
    return raw.get("value")


def cache_put(namespace: str, key: str, value: Any) -> None:
    """Persist a value. A cache write failure must not fail the request."""
    path = _path(namespace, key)
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a reader must never see a half-written file.
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"at": time.time(), "key": key, "value": value}, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(path)
    except (OSError, TypeError):
        pass


def cached(
    namespace: str,
    key: str,
    producer: Callable[[], Any],
    ttl: float = DEFAULT_TTL_SEC,
) -> Any:
    """Return the cached value, or produce it and keep it across restarts.

    A failed production (``ok`` false) is deliberately not cached: caching a
    "key not configured" answer would keep returning it after the key was
    added, and caching a transport failure would make one bad minute stick for
    a week.
    """
    hit = cache_get(namespace, key, ttl)
    if hit is not None:
        return hit
    value = producer()
    if isinstance(value, dict) and value.get("ok") is False:
        return value
    cache_put(namespace, key, value)
    return value


def cache_stats() -> dict[str, Any]:
    """What is cached, so the console can show what a restart no longer costs."""
    namespaces: dict[str, dict[str, Any]] = {}
    if not CACHE_DIR.exists():
        return {"entries": 0, "namespaces": {}, "dir": str(CACHE_DIR)}
    total = 0
    for child in sorted(CACHE_DIR.iterdir()):
        if not child.is_dir():
            continue
        files = list(child.glob("*.json"))
        total += len(files)
        newest = max((f.stat().st_mtime for f in files), default=0)
        namespaces[child.name] = {
            "entries": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "newest_age_sec": round(time.time() - newest) if newest else None,
        }
    return {"entries": total, "namespaces": namespaces, "dir": str(CACHE_DIR)}


def clear(namespace: str | None = None) -> int:
    """Drop cached entries. Returns how many files were removed."""
    target = CACHE_DIR / namespace if namespace else CACHE_DIR
    if not target.exists():
        return 0
    removed = 0
    for path in target.rglob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
