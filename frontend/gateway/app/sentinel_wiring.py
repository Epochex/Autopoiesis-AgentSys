"""Binds the sentinel to this deployment's detectors and action set.

Kept out of both the sentinel and the detectors so neither imports the gateway:
the loop is generic, the detectors are domain knowledge, and only this file
knows which of the two are wired together on this box.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from core.remediate.sentinel import Sentinel
from domains.network_rca.detectors import ALL_DETECTORS

_sentinel: Sentinel | None = None
_lock = threading.Lock()


def _build() -> Sentinel:
    from .remediation import execute, preflight

    return Sentinel(
        detectors=list(ALL_DETECTORS),
        execute=execute,
        preflight=preflight,
        interval_sec=float(os.getenv("AUTOPOIESIS_SENTINEL_INTERVAL", "20")),
    )


def get_sentinel() -> Sentinel:
    global _sentinel
    with _lock:
        if _sentinel is None:
            _sentinel = _build()
        return _sentinel


def poll_once() -> dict[str, Any]:
    return get_sentinel().poll_once()


def start_background() -> None:
    """Run the loop in a daemon thread. Off unless explicitly enabled.

    Autonomous action on a live box is opt-in for the same reason the model
    prewarm is: something that acts on its own should never arrive as a side
    effect of a deploy.
    """
    if os.getenv("AUTOPOIESIS_SENTINEL", "0") != "1":
        return
    threading.Thread(target=get_sentinel().run_forever, daemon=True).start()
