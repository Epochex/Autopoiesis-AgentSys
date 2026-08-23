"""Binds the sentinel to this deployment's detectors and action set.

Kept out of both the sentinel and the detectors so neither imports the gateway:
the loop is generic, the detectors are domain knowledge, and only this file
knows which of the two are wired together on this box.
"""

from __future__ import annotations

import inspect
import os
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from core.env import autopoiesis_env
from core.remediate.sentinel import Sentinel, record, timeline
from domains.network_rca.detectors import ALL_DETECTORS
from domains.network_rca.incident_memory import consolidate_incident_timeline
from domains.network_rca.incident_memory import completed_incident_chains
from domains.network_rca.incident_dossier import from_sentinel_chain

_sentinel: Sentinel | None = None
_lock = threading.Lock()


def _resolve_learning_service() -> Any | None:
    """Find the shared store only after the gateway has finished building it."""
    if not autopoiesis_env("MEMORY_DSN"):
        return None
    from . import main

    service = getattr(main, "_evolving_service", None)
    if service is None or service.memory.repository is None:
        return None
    return service


def _remember_completed_incidents() -> None:
    rows = timeline(2000)
    from . import main

    operational = getattr(main, "_operational_memory", None)
    if operational is not None:
        for chain in completed_incident_chains(rows):
            operational.save_dossier(from_sentinel_chain(chain, source_mode="live"))

    service = _resolve_learning_service()
    if service is None:
        return
    request_lock = getattr(service, "_request_lock", None)
    with request_lock if request_lock is not None else nullcontext():
        consolidate_incident_timeline(rows, service.memory, service.skills)
        apply_retention = getattr(service, "_apply_memory_retention", None)
        if apply_retention is not None:
            # Diagnose applies retention after consolidation, while sentinel
            # writes bypass that path. Keep both mutations inside the request
            # lock already held here so every production writer enforces the
            # same decay and capacity policy without acquiring the lock twice.
            apply_retention(now=datetime.now(timezone.utc))
            service.memory.flush()


class _LearningSentinel(Sentinel):
    def poll_once(self) -> dict[str, Any]:
        result = super().poll_once()
        try:
            _remember_completed_incidents()
        except Exception:
            # The disposition is already durable in the append-only timeline.
            # A store outage must leave the autonomous safety loop available;
            # the stable run id lets a later poll retry without double counting.
            pass
        return result


def _build() -> Sentinel:
    from .remediation import execute, preflight

    def execute_with_timeline(
        action: str,
        target: str,
        on_command=None,
    ) -> dict[str, Any]:
        def emit(kind: str, payload: dict[str, Any]) -> None:
            enriched = dict(payload)
            if enriched.get("action") and enriched["action"] != action:
                enriched["followup_action"] = enriched["action"]
            enriched["subject"] = target
            enriched["action"] = action
            record(kind, enriched)

        context: dict[str, Any] = {"emit": emit, "on_command": on_command}
        # Small test/deployment adapters may expose the older two-argument
        # contract. The production executor declares these fields explicitly;
        # inspect before adding them so compatibility does not rely on catching
        # a TypeError raised from inside the action.
        parameters = inspect.signature(execute).parameters
        if "incident_id" in parameters:
            context["incident_id"] = f"sentinel:{action}:{target}"
        if "idempotency_key" in parameters:
            context["idempotency_key"] = datetime.now(timezone.utc).isoformat()
        return execute(action, target, **context)

    return _LearningSentinel(
        detectors=list(ALL_DETECTORS),
        execute=execute_with_timeline,
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
    sentinel = get_sentinel()
    # The gateway really was restarted by a CI deploy while execute() was
    # blocked in its watch window. Reconcile before the new polling thread can
    # act, so the old action is visible and its target is already cooling when
    # the first detector result arrives.
    sentinel.reconcile_interrupted_watches()
    threading.Thread(target=sentinel.run_forever, daemon=True).start()
