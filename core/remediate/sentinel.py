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

from core.remediate import recurrence

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
    return Path("/data/autopoiesis-production/sentinel-timeline.jsonl")


TIMELINE_PATH = _default_timeline()

# How long a target stays untouchable after the sentinel acted on it. Long
# enough that a flapping service is escalated rather than restarted in a loop.
COOLDOWN_SEC = float(os.getenv("AUTOPOIESIS_SENTINEL_COOLDOWN", "600"))

# Consecutive detections before acting. One poll can catch a service mid-restart
# during a deploy; two in a row is the system telling you something.
CONFIRM_POLLS = int(os.getenv("AUTOPOIESIS_SENTINEL_CONFIRM", "2"))

_WATCH_OPEN_KINDS = frozenset({"remediation_committed", "bakein_opened"})
_WATCH_TERMINAL_KINDS = frozenset({
    "remediated",
    "resolved",
    "bakein_passed",
    "bakein_regressed",
    "remediation_reverted",
})
_INTERRUPTED_NOTE = (
    "动作已执行但观察期没走完（进程在中途重启）。"
    "这次改动没有被回读验证过，需要人确认它站住了没有。"
)


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
    # A write that could mitigate the symptom but has not earned automatic
    # execution. Keeping it separate from ``action`` is the safety boundary:
    # the sentinel may explain the withheld write, but cannot execute it.
    candidate_action: str | None = None
    safety_reason: str | None = None

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
            "candidate_action": self.candidate_action,
            "safety_reason": self.safety_reason,
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


def timeline(limit: int | None = 200) -> list[dict[str, Any]]:
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
    return rows if limit is None else rows[-limit:]


@dataclass
class _OpenWatch:
    """The durable part of one watch that had not reached a disposition."""

    subject: str
    detector: str
    action: str
    committed_at: str | None = None
    opened_at: str | None = None
    last_sample_at: str | None = None
    samples_seen: int = 0

    @property
    def identity(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.detector,
            self.subject,
            self.action,
            self.committed_at or "",
            self.last_sample_at or "",
            self.samples_seen,
        )

    def interrupted_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "detector": self.detector,
            "action": self.action,
            "committed_at": self.committed_at,
            "last_sample_at": self.last_sample_at,
            "samples_seen": self.samples_seen,
            "note": _INTERRUPTED_NOTE,
        }


def _watch_route(event: dict[str, Any]) -> tuple[str, str] | None:
    subject = str(event.get("subject") or "")
    action = str(event.get("action") or "")
    return (subject, action) if subject and action else None


def _interrupted_identity(event: dict[str, Any]) -> tuple[str, str, str, str, str, int]:
    return (
        str(event.get("detector") or ""),
        str(event.get("subject") or ""),
        str(event.get("action") or ""),
        str(event.get("committed_at") or ""),
        str(event.get("last_sample_at") or ""),
        int(event.get("samples_seen") or 0),
    )


def _unfinished_watches(
    events: list[dict[str, Any]],
) -> tuple[list[_OpenWatch], list[dict[str, Any]]]:
    """Fold process generations into watches that never reached a terminal."""
    active: dict[tuple[str, str], _OpenWatch] = {}
    unfinished: list[_OpenWatch] = []
    announced: list[dict[str, Any]] = []
    last_detector: dict[tuple[str, str], str] = {}

    for event in events:
        kind = str(event.get("kind") or "")
        route = _watch_route(event)
        detector = str(event.get("detector") or "")
        if route is not None and detector:
            last_detector[route] = detector

        if kind == "sentinel_started":
            # A CI deploy produced this exact boundary in production: the last
            # line was bakein_sampled, then the gateway disappeared, and the
            # next process wrote sentinel_started. The killed process had no
            # opportunity to append a finalizer, so the next process must fold
            # the durable log instead of depending on an exit hook.
            unfinished.extend(active.values())
            active.clear()
            continue

        if kind == "watch_interrupted":
            announced.append(event)
            if route is not None:
                current = active.get(route)
                if current is not None and current.identity == _interrupted_identity(event):
                    active.pop(route)
            continue

        if kind in _WATCH_OPEN_KINDS and route is not None:
            current = active.get(route)
            starts_another = current is not None and (
                (kind == "remediation_committed" and current.committed_at is not None)
                or (kind == "bakein_opened" and current.opened_at is not None)
            )
            if starts_another:
                unfinished.append(current)
                current = None
            if current is None:
                current = _OpenWatch(
                    subject=route[0],
                    action=route[1],
                    detector=detector or last_detector.get(route, ""),
                )
                active[route] = current
            elif detector:
                current.detector = detector
            if kind == "remediation_committed":
                current.committed_at = str(event.get("at") or "") or None
            else:
                current.opened_at = str(event.get("at") or "") or None
            continue

        if kind == "bakein_sampled" and route in active:
            current = active[route]
            current.samples_seen += 1
            current.last_sample_at = str(event.get("at") or "") or None
            continue

        if kind in _WATCH_TERMINAL_KINDS and route is not None:
            active.pop(route, None)

    unfinished.extend(active.values())
    already_announced = {_interrupted_identity(event) for event in announced}
    pending: list[_OpenWatch] = []
    seen: set[tuple[str, str, str, str, str, int]] = set()
    for watch in unfinished:
        if watch.identity not in already_announced and watch.identity not in seen:
            pending.append(watch)
            seen.add(watch.identity)
    return pending, announced


@dataclass
class Sentinel:
    """Polls detectors and drives the graded pipeline when one fires."""

    detectors: list[Callable[[], list[Detection]]]
    # Both take an optional `on_command=` sink, called with each command as
    # it runs, so the transcript reaches the timeline while the phase is live.
    execute: Callable[..., dict[str, Any]]
    preflight: Callable[..., dict[str, Any]]
    interval_sec: float = 20.0
    cooldown_sec: float = COOLDOWN_SEC
    confirm_polls: int = CONFIRM_POLLS
    clock: Callable[[], float] = time.monotonic

    _streak: dict[str, int] = field(default_factory=dict)
    # Keys already announced as escalated, so a fault that stays broken does not
    # re-announce every fifteen seconds. Cleared when the condition clears, and
    # deliberately not persisted: a fresh process should say it again once.
    _escalated: set[str] = field(default_factory=set)
    _cooldown_until: dict[str, float] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    # The timer, the console's "poll now" control and rehearsal scripts all
    # reach the same Sentinel instance.  Only one may advance confirmation,
    # consume budgets or execute an action at a time; a duplicate poll is a
    # duplicate write decision, not harmless extra observation.
    _poll_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reconcile_interrupted_watches(self) -> list[dict[str, Any]]:
        """Close watches abandoned by an earlier process and restore cooling.

        This is level-triggered startup work. SIGKILL, OOM, and power loss do
        not run cleanup code; the next process can only trust the timeline that
        survived them. Replaying the whole file also catches an older process
        boundary that was missed before this reconciler existed.
        """
        try:
            pending, announced = _unfinished_watches(timeline(None))
        except OSError:
            return []

        written = [
            record("watch_interrupted", watch.interrupted_payload())
            for watch in pending
        ]
        interruptions = [*announced, *written]
        wall_now = datetime.now(timezone.utc).timestamp()
        monotonic_now = self.clock()
        for event in interruptions:
            detector = str(event.get("detector") or "")
            subject = str(event.get("subject") or "")
            if not detector or not subject:
                continue
            try:
                interrupted_at = datetime.fromisoformat(str(event.get("at") or "")).timestamp()
                remaining = self.cooldown_sec - max(0.0, wall_now - interrupted_at)
            except (TypeError, ValueError):
                remaining = self.cooldown_sec
            if remaining <= 0:
                continue
            key = f"{detector}:{subject}"
            self._cooldown_until[key] = max(
                self._cooldown_until.get(key, 0.0), monotonic_now + remaining
            )
        return written

    def poll_once(
        self,
        detectors: list[Callable[[], list[Detection]]] | None = None,
        *,
        blocking: bool = True,
    ) -> dict[str, Any]:
        """One serialized detect-decide-act cycle.

        ``detectors`` narrows an operator-requested observation without
        widening the action catalogue.  ``blocking=False`` is used by HTTP
        callers so an in-progress watch returns ``busy`` immediately instead
        of accumulating another cycle behind it.
        """
        if not self._poll_lock.acquire(blocking=blocking):
            return {"detections": [], "acted": [], "busy": True}
        try:
            return self._poll_once_locked(detectors or self.detectors)
        finally:
            self._poll_lock.release()

    def _poll_once_locked(
        self,
        detectors: list[Callable[[], list[Detection]]],
    ) -> dict[str, Any]:
        """Advance state while ``_poll_lock`` is held."""
        seen: list[Detection] = []
        for detector in detectors:
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
        # An escalation ends when the condition does. Recording that is not
        # bookkeeping: the in-process latch is invisible to anything reading the
        # log, so without this line a card stays "needs a person" forever after
        # the person fixed it — the system would be unable to notice it had been
        # helped.
        for key in sorted(self._escalated - active):
            detector, _, rest = key.partition(":")
            subject, _, action = rest.rpartition(":")
            record("escalation_cleared", {
                "subject": subject, "detector": detector, "action": action or None,
                "note": "该目标已不再报错，升级状态解除；复发计数仍在窗口内保留",
            })
        self._escalated &= active

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
                    "subject": detection.subject,
                    "detector": detection.detector,
                    "family": detection.family,
                    "candidate_action": detection.candidate_action,
                    "decision": "retained_not_executed",
                    "reason": detection.safety_reason or (
                        "当前没有通过安全门的预注册动作；写操作保持不变，"
                        "事件证据已记录并转人工。"
                    ),
                })
                continue

            history = self._history(detection)
            if history.escalate:
                if detection.key not in self._escalated:
                    self._escalated.add(detection.key)
                    record("escalated", {
                        "subject": detection.subject,
                        "detector": detection.detector,
                        "action": detection.action,
                        "recurrences": history.recurrences,
                        "window_hours": int(recurrence.WINDOW_SEC // 3600) or 1,
                        # The chain of prior repairs that caused this refusal —
                        # the answer to "why did it stop?" belongs next to the
                        # decision, not in a doc.
                        "prior_cycles": [c.as_dict() for c in history.cycles],
                        "reason": recurrence.escalation_note(history),
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
        return {"detections": [d.as_dict() for d in seen], "acted": acted, "busy": False}

    def _history(self, detection: Detection) -> recurrence.History:
        """How often this exact repair has already worked and not held.

        Read from the timeline every time rather than kept alongside the loop's
        own counters: a separately-maintained tally can drift from the audit
        trail it claims to summarise, and one that disagrees with the evidence
        is worse than none — it is wrong where nobody can see it.
        """
        return recurrence.history_for(
            detection.detector, detection.target or detection.subject, detection.action
        )

    def _act(self, detection: Detection) -> dict[str, Any]:
        """Preflight, execute under the watch window, and record the verdict."""
        action, target = detection.action, detection.target or detection.subject

        def log_command(entry: dict[str, Any]) -> None:
            """One timeline line per command, as it runs.

            Streamed rather than attached at the end of the phase: the watch
            window is ninety seconds long, and a transcript that only appears
            once the window closes cannot be read while it is happening.
            """
            record("command", {"subject": target, "action": action,
                               "argv": entry["argv"], "rc": entry["rc"],
                               "out": entry["out"], "truncated": entry["truncated"]})

        check = self.preflight(action, target, on_command=log_command)
        record("preflight", {"subject": target, "action": action,
                             "eligible": check.get("eligible"),
                             "reason": check.get("reason"),
                             "blast_radius": check.get("blast_radius")})
        if not check.get("eligible"):
            record("declined", {"subject": target, "action": action,
                                "reason": check.get("reason", "preconditions not met")})
            return {"subject": target, "action": action, "outcome": "declined"}

        # Each failed-to-hold repair buys the next one a longer quiet period —
        # fail2ban's bantime.multipliers, doubling. Room for a slower real cause
        # to surface instead of being papered over on a fixed cadence.
        history = self._history(detection)
        self._cooldown_until[detection.key] = self.clock() + recurrence.cooldown_for(
            history.recurrences, self.cooldown_sec
        )
        result = self.execute(action, target, on_command=log_command)
        verdict = (result or {}).get("verdict") or {}
        record("remediated", {
            "subject": target, "action": action, "detector": detection.detector,
            "outcome": verdict.get("outcome") or ("refused" if result.get("refused") else "unknown"),
            "needs_human": result.get("needs_human"),
            "detail": result.get("detail") or result.get("reason"),
            "samples": len(verdict.get("samples") or []),
            "fast_samples": verdict.get("fast_samples"),
            "stability_samples": verdict.get("stability_samples"),
            "target_recovered": verdict.get("target_recovered"),
            "baseline": verdict.get("baseline"),
            "execution_id": result.get("execution_id"),
            "incident_id": result.get("incident_id"),
            "failure_domain": result.get("failure_domain"),
            "budget_decision": result.get("budget_decision"),
            "recovery_run": result.get("recovery_run"),
        })
        # The streak clears — the fault is gone, so a return has to re-confirm
        # from scratch. The cooldown does NOT clear, and that is the change: it
        # was set above to a length that grows with how often this same repair
        # has already failed to hold. Clearing it on success was what made
        # recurrence invisible; the fix would work, the fault would come back
        # twenty minutes later, and nothing remembered.
        if verdict.get("outcome") == "passed":
            self._streak.pop(detection.key, None)
            # detector + action are what the recurrence projection keys on; a
            # resolution that does not say what it resolved cannot be counted.
            record("resolved", {"subject": target, "action": action,
                                "detector": detection.detector,
                                "outcome": verdict.get("outcome"),
                                "samples": len(verdict.get("samples") or []),
                                "fast_window_seconds": verdict.get("window_seconds"),
                                "stability_window_seconds": verdict.get("stability_window_seconds"),
                                "target_recovered": verdict.get("target_recovered"),
                                "execution_id": result.get("execution_id"),
                                "recovery_state": (result.get("recovery_run") or {}).get("state"),
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
