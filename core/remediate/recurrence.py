"""What has already been fixed, and came back anyway.

The sentinel used to forget. On a verified pass it deleted both the streak and
the cooldown for that target, so a service that failed, was restarted, and
failed again twenty minutes later got restarted again with no record that any
of it had happened. Both counters lived in process memory, so a deploy wiped
them too. That is the most permissive reset rule of any comparable mechanism:
systemd wants a human to run `reset-failed`, Kubernetes wants ten minutes of
health, Pacemaker clears its fail count all-or-nothing and only after a quiet
period.

Forgetting like that is not merely a missing feature. Bainbridge's 1983
*Ironies of Automation* names the failure exactly: automatic control
"camouflages" a fault by correcting against it, so the trend stays invisible
until it is past saving. An interface restarted forty times in a day — every
restart "successful" — is that paper's example with a MAC address attached.

So this module answers one question: *has this exact remediation already worked
on this exact target, and did the problem come back?* It is an aggregation over
the append-only timeline, nothing more. No embedding, no model, no retrieval —
which is the point. When someone asks how you know the memory is what changed
the decision, the answer should be a query you can read aloud.

Deliberately NOT built here: a decaying penalty score. BGP route flap damping
is the mature version of that idea, and RFC 7196 is fifteen years of evidence
that its constants are hard to get right — the original defaults punished
well-connected sites so badly that operators disabled the feature. A sliding
window you can explain in one sentence beats a score you cannot defend.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# How far back a recurrence still counts. A day: long enough to catch "this
# keeps happening" on a service that fails a few times per shift, short enough
# that last week's resolved incident does not block today's repair.
WINDOW_SEC = float(os.getenv("AUTOPOIESIS_RECURRENCE_WINDOW", str(24 * 3600)))

# Recurrences tolerated before the action is refused and a person is asked for.
# Three, because two could be coincidence and the fourth restart is the one that
# would have been wasted. systemd's StartLimitBurst is 5, Kubernetes KEP-5734
# uses 7 — those count raw restarts, where this counts fixed-then-broke-again
# cycles, which is a much stronger signal per unit.
LIMIT = int(os.getenv("AUTOPOIESIS_RECURRENCE_LIMIT", "3"))

# Ceiling on the escalating cooldown, so the doubling cannot park a target out
# of reach for a day on its way to the limit.
COOLDOWN_MAX_SEC = float(os.getenv("AUTOPOIESIS_RECURRENCE_COOLDOWN_MAX", str(4 * 3600)))


@dataclass(frozen=True)
class Cycle:
    """One completed round of "we fixed it and it came back"."""

    at: str
    outcome: str
    samples: int

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "outcome": self.outcome, "samples": self.samples}


@dataclass(frozen=True)
class History:
    """Everything the timeline knows about one (detector, subject, action)."""

    key: str
    cycles: tuple[Cycle, ...] = ()
    last_resolved_at: str | None = None

    @property
    def recurrences(self) -> int:
        return len(self.cycles)

    @property
    def escalate(self) -> bool:
        return should_escalate(self.recurrences)


def key_of(detector: str, subject: str, action: str | None) -> str:
    """Identity for the counter.

    Keyed on the action too, not just the target: "restarting X keeps not
    sticking" and "X keeps having different problems" are different findings and
    must not share a counter. Pacemaker draws the same line between a start
    failure and a runtime failure.
    """
    return f"{detector}:{subject}:{action or '-'}"


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _event_key(event: Mapping[str, Any]) -> str | None:
    detector = str(event.get("detector") or "")
    subject = str(event.get("subject") or "")
    if not subject:
        return None
    action = event.get("action")
    if detector:
        return key_of(detector, subject, action if action is None else str(action))
    return None


def project(
    events: Iterable[Mapping[str, Any]], *, now: float, window_sec: float = WINDOW_SEC
) -> dict[str, History]:
    """Fold the timeline into per-key recurrence history.

    Pure: no clock, no disk, no environment. That is what makes the count
    testable without fixtures, replayable in a demo, and checkable by anyone who
    doubts the number — they can run this over the same JSONL and compare.

    A cycle is closed by `resolved`, and only counts once a LATER `detected`
    proves it came back. A fix still holding is not a recurrence, and a fix that
    never took hold is a different signal entirely (it never reaches `resolved`).
    """
    cutoff = now - window_sec
    # `detected` carries the action; `resolved` carries it too, but the events
    # between them may not — so the key is resolved from whichever event has it.
    pending: dict[str, Cycle] = {}
    closed: dict[str, list[Cycle]] = {}
    last_resolved: dict[str, str] = {}

    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in {"detected", "resolved"}:
            continue
        key = _event_key(event)
        if key is None:
            continue
        at = str(event.get("at") or "")

        if kind == "resolved":
            # Held, not counted: a fix only becomes a recurrence when the fault
            # returns. Until then this is a success, and success is not evidence
            # that anything is wrong.
            pending[key] = Cycle(
                at=at,
                outcome=str(event.get("outcome") or "passed"),
                samples=int(event.get("samples") or 0),
            )
            last_resolved[key] = at
            continue

        # kind == "detected": if this key had a held resolution, the fix did not
        # hold, and that pair is one recurrence.
        held = pending.pop(key, None)
        if held is not None:
            closed.setdefault(key, []).append(held)

    return {
        key: History(
            key=key,
            cycles=tuple(c for c in cycles if _epoch(c.at) >= cutoff),
            last_resolved_at=last_resolved.get(key),
        )
        for key, cycles in closed.items()
    }


# Read the tail in chunks this size when walking backwards to the window edge.
_CHUNK = 256 * 1024


def _events_since(path: Any, cutoff: float) -> list[dict[str, Any]]:
    """Timeline entries at or after `cutoff`, oldest first.

    Bounded by the WINDOW, not by a line count. The obvious implementation —
    take the last N lines — is wrong here and quietly so: the loop appends a
    `cycle` entry every poll, which at a fifteen-second interval is 5,760 lines
    a day, so a 4,000-line tail covers about eleven hours. A counter that says
    "3 times in 24 hours" while actually looking at eleven is not a small
    inaccuracy; it is a number on a screen that cannot be reproduced from the
    log it claims to summarise.
    """
    import json
    import os

    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    chunks: list[bytes] = []
    read = 0
    reached_cutoff = False
    try:
        with open(path, "rb") as handle:
            while read < size and not reached_cutoff:
                step = min(_CHUNK, size - read)
                read += step
                handle.seek(size - read)
                chunks.insert(0, handle.read(step))
                # Only decide on a chunk boundary, and only on a line we can
                # parse — a partial first line is completed by the next chunk.
                head = b"".join(chunks).split(b"\n", 1)[0]
                try:
                    first = json.loads(head)
                except (ValueError, UnicodeDecodeError):
                    continue
                reached_cutoff = _epoch(str(first.get("at") or "")) < cutoff
    except OSError:
        return []

    out: list[dict[str, Any]] = []
    for line in b"".join(chunks).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a torn first line, or the sentinel mid-write at the end
        if isinstance(event, dict):
            out.append(event)
    return out


def history_for(
    detector: str,
    subject: str,
    action: str | None,
    *,
    now: float | None = None,
    window_sec: float = WINDOW_SEC,
) -> History:
    """Read the live timeline and project one key's history out of it.

    Rebuilt from the log every time rather than kept as mutable state. A
    separately-maintained counter can drift from the audit trail it claims to
    summarise, and one that silently disagrees with the evidence is worse than
    none — it is wrong where nobody can see it.
    """
    import time

    from core.remediate.sentinel import _default_timeline

    key = key_of(detector, subject, action)
    now = time.time() if now is None else now
    events = _events_since(_default_timeline(), now - window_sec)
    return project(events, now=now, window_sec=window_sec).get(key) or History(key=key)


def cooldown_for(recurrences: int, base_sec: float, *, cap_sec: float = COOLDOWN_MAX_SEC) -> float:
    """Escalating quiet period — fail2ban's `bantime.multipliers`, doubling.

    Each time a fix fails to hold, wait longer before trying it again. This buys
    room for a slower real cause to show itself instead of being papered over at
    a fixed cadence.
    """
    if recurrences <= 0:
        return base_sec
    return min(base_sec * (2 ** recurrences), cap_sec)


def should_escalate(recurrences: int, *, limit: int = LIMIT) -> bool:
    return recurrences >= limit


def window_label(window_sec: float = WINDOW_SEC) -> str:
    """The window, in a unit that does not round a real number into a wrong one.

    `int(window_sec // 3600) or 1` printed "1 小时" for every window under an
    hour — including the compressed ones a demo runs on. Putting a number on a
    screen that the log will not support is exactly the thing this whole
    mechanism is supposed to avoid.
    """
    if window_sec >= 3600:
        hours = window_sec / 3600
        return f"{hours:.0f} 小时" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} 小时"
    if window_sec >= 60:
        return f"{window_sec / 60:.0f} 分钟"
    return f"{window_sec:.0f} 秒"


def escalation_note(history: History, *, window_sec: float = WINDOW_SEC) -> str:
    return (
        f"同一处置在 {window_label(window_sec)}内已有 {history.recurrences} 次通过回读，随后同一故障再次出现；"
        "已达到复发预算，停止重复执行并转人工排查持续性原因。"
    )
