"""The loop that notices, decides, acts, and proves it worked.

Everything before this was operator-triggered: a person looked, a person asked,
a person pressed. The sentinel closes that — it polls a small set of detectors,
and when one fires it runs the same graded pipeline a person would have run,
under the same preconditions and the same watch window.

What it deliberately does not do:

**It never widens the action set.** The sentinel can only run what is already
in the monotonic allowlist. A detector that fires on something with no safe
action produces an alert and stops there; it does not improvise.

**It never acts twice on the same thing.** A target that was acted on and did
not recover goes on a cooldown, because the failure mode of an autonomous loop
is not a wrong action, it is the same action repeated forever.

**It records the whole chain, including what it declined.** A cycle where
nothing fired is still a cycle; a detection that was refused is still a
detection. An audit trail with only successes in it is a sales brochure.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _default_timeline() -> Path:
    """Where the sentinel writes what it saw and did.

    Under pytest this moves to a temp path. A test run writing fabricated
    detections into the live timeline puts `demo.service` and a deliberately
    broken detector next to real incidents — and the timeline is the audit
    trail, so polluting it is worse than not having one.
    """
    configured = os.getenv("AUTOPOIESIS_SENTINEL_TIMELINE")
    if configured:
        return Path(configured)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(tempfile.gettempdir()) / "autopoiesis-sentinel-test.jsonl"
    return Path("/data/autopoiesis-runtime/sentinel-timeline.jsonl")


TIMELINE_PATH = _default_timeline()

# How long a target stays untouchable after the sentinel acted on it. Long
# enough that a flapping service is escalated rather than restarted in a loop.
COOLDOWN_SEC = float(os.getenv("AUTOPOIESIS_SENTINEL_COOLDOWN", "600"))

# Consecutive detections before acting. One poll can catch a service mid-restart
# during a deploy; two in a row is the system telling you something.
CONFIRM_POLLS = int(os.getenv("AUTOPOIESIS_SENTINEL_CONFIRM", "2"))


@dataclass
class Detection:
    """Something a detector saw, and the action it maps to — if any."""

    detector: str
    family: str
    subject: str
    severity: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    action: str | None = None
    target: str | None = None

    @property
    def key(self) -> str:
        return f"{self.detector}:{self.subject}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "family": self.family,
            "subject": self.subject,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
            "action": self.action,
            "target": self.target,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one entry to the timeline the console replays."""
    entry = {"at": _now(), "kind": kind, **payload}
    path = _default_timeline()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
    return entry


def timeline(limit: int = 200) -> list[dict[str, Any]]:
    """The chain of what was seen, decided, done and verified — newest last."""
    path = _default_timeline()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


@dataclass
class Sentinel:
    """Polls detectors and drives the graded pipeline when one fires."""

    detectors: list[Callable[[], list[Detection]]]
    execute: Callable[[str, str], dict[str, Any]]
    preflight: Callable[[str, str], dict[str, Any]]
    interval_sec: float = 20.0
    cooldown_sec: float = COOLDOWN_SEC
    confirm_polls: int = CONFIRM_POLLS
    clock: Callable[[], float] = time.monotonic

    _streak: dict[str, int] = field(default_factory=dict)
    _cooldown_until: dict[str, float] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)

    def poll_once(self) -> dict[str, Any]:
        """One full cycle: detect, decide, act, and record every branch."""
        seen: list[Detection] = []
        for detector in self.detectors:
            try:
                seen.extend(detector())
            except Exception as error:  # noqa: BLE001 - a broken detector is a finding
                record("detector_failed", {"error": f"{type(error).__name__}: {error}"[:200]})

        active = {detection.key for detection in seen}
        # A condition that cleared resets its streak, so an intermittent fault
        # has to re-confirm rather than accumulating across hours.
        for key in list(self._streak):
            if key not in active:
                del self._streak[key]

        acted: list[dict[str, Any]] = []
        for detection in seen:
            self._streak[detection.key] = self._streak.get(detection.key, 0) + 1
            streak = self._streak[detection.key]
            entry = record("detected", {**detection.as_dict(), "streak": streak})

            if streak < self.confirm_polls:
                record("awaiting_confirmation", {
                    "subject": detection.subject, "detector": detection.detector,
                    "streak": streak, "need": self.confirm_polls,
                    "note": "一次采样可能撞上部署中的瞬时状态，等下一轮再确认",
                })
                continue

            if detection.action is None:
                record("no_safe_action", {
                    "subject": detection.subject, "family": detection.family,
                    "note": "这一族没有可自动执行的动作，只出告警等人处理",
                })
                continue

            until = self._cooldown_until.get(detection.key, 0.0)
            if self.clock() < until:
                record("cooldown", {
                    "subject": detection.subject,
                    "remaining_sec": round(until - self.clock()),
                    "note": "刚处置过还没恢复，不再重复动作，避免把一次降级放大成故障",
                })
                continue

            acted.append(self._act(detection))

        record("cycle", {"detections": len(seen), "acted": len(acted)})
        return {"detections": [d.as_dict() for d in seen], "acted": acted}

    def _act(self, detection: Detection) -> dict[str, Any]:
        """Preflight, execute under the watch window, and record the verdict."""
        action, target = detection.action, detection.target or detection.subject
        check = self.preflight(action, target)
        record("preflight", {"subject": target, "action": action,
                             "eligible": check.get("eligible"),
                             "reason": check.get("reason"),
                             "blast_radius": check.get("blast_radius")})
        if not check.get("eligible"):
            record("declined", {"subject": target, "action": action,
                                "reason": check.get("reason", "preconditions not met")})
            return {"subject": target, "action": action, "outcome": "declined"}

        self._cooldown_until[detection.key] = self.clock() + self.cooldown_sec
        result = self.execute(action, target)
        verdict = (result or {}).get("verdict") or {}
        record("remediated", {
            "subject": target, "action": action,
            "outcome": verdict.get("outcome") or ("refused" if result.get("refused") else "unknown"),
            "needs_human": result.get("needs_human"),
            "detail": result.get("detail") or result.get("reason"),
            "samples": len(verdict.get("samples") or []),
            "baseline": verdict.get("baseline"),
        })
        # A pass clears the cooldown: the thing is fixed, and a later unrelated
        # fault on the same target should not be ignored for ten minutes.
        if verdict.get("outcome") == "passed":
            self._cooldown_until.pop(detection.key, None)
            self._streak.pop(detection.key, None)
            record("resolved", {"subject": target, "action": action,
                                "note": "改完回读通过，观察期内没有回归"})
        return {"subject": target, "action": action,
                "outcome": verdict.get("outcome"), "needs_human": result.get("needs_human")}

    def run_forever(self) -> None:
        """Poll until stopped. Intended for a daemon thread."""
        record("sentinel_started", {"interval_sec": self.interval_sec,
                                    "confirm_polls": self.confirm_polls,
                                    "cooldown_sec": self.cooldown_sec})
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as error:  # noqa: BLE001 - the loop must outlive one bad cycle
                record("cycle_failed", {"error": f"{type(error).__name__}: {error}"[:200]})
            self._stop.wait(self.interval_sec)
        record("sentinel_stopped", {})

    def stop(self) -> None:
        self._stop.set()
