"""Post-remediation follow-up: commit, watch, and revert on regression.

A write that reads back correctly is not yet a fix. The state can be right at
t+0 and wrong at t+90s — the interface comes back up and drops again, the
restarted collector dies a second time, the freed disk refills. So a committed
change here enters a watch window and only then closes the case.

Three properties carry the design:

**Level-triggered.** Every sample re-reads the live system. Nothing trusts the
action's return value, or the previous sample, or a cached view. This is the
Kubernetes controller model — reconcile from observed state, never from the
event that fired — and it is the only way a watcher notices a change that
undid itself quietly.

**Baseline-relative.** The window opens with a reading taken *before* the
action. A probe that was already failing before the fix, and is still failing
after, is not a regression caused by the fix; a probe that was passing and now
fails is. Judging against an absolute "healthy" bar instead of the baseline
makes the watcher blame the remediation for whatever was already broken.

**Reverts are verified, never assumed.** A revert that ran without raising is
still unproven. The watcher reverts, re-reads, compares against the baseline,
and if they still disagree it reports ``revert_unverified`` and escalates
rather than reporting success. Silence is not proof.

Only monotonic actions belong here — the target is already at its worst, so the
change cannot make things worse than the baseline it is measured against. The
caller asserts that by passing ``monotonic=True``; the contract layer upstream
is what enforces it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Outcome = Literal["passed", "reverted", "revert_unverified", "not_committed"]


@dataclass
class HealthProbe:
    """One thing worth watching after a change, and how to judge a reading.

    ``read`` must hit the live system every call. Returning a memoised value
    defeats the whole mechanism: the watcher would sample the same stale
    reading for the length of the window and always pass.
    """

    name: str
    read: Callable[[], dict[str, Any]]
    healthy: Callable[[dict[str, Any]], bool]

    def sample(self) -> tuple[dict[str, Any], bool]:
        reading = dict(self.read())
        return reading, bool(self.healthy(reading))


class Sample(BaseModel):
    probe: str
    at: datetime
    reading: dict[str, Any]
    healthy: bool
    regressed: bool = False


class FollowUpVerdict(BaseModel):
    """What the watch window concluded, with every reading it took."""

    action: str
    outcome: Outcome
    committed: bool
    baseline: dict[str, bool] = Field(default_factory=dict)
    samples: list[Sample] = Field(default_factory=list)
    regressed_probes: list[str] = Field(default_factory=list)
    window_seconds: float = 0.0
    detail: str = ""

    @property
    def needs_human(self) -> bool:
        """A revert we could not prove is the one case a person must look at."""
        return self.outcome == "revert_unverified"


@dataclass
class BakeIn:
    """Watch window parameters.

    ``window_seconds`` is a ceiling, not a target: the watcher stops early the
    moment a regression appears. ``grace_seconds`` skips the settling period
    right after a change, where a bouncing interface or a restarting unit is
    expected to read unhealthy for a moment.
    """

    window_seconds: float = 300.0
    interval_seconds: float = 15.0
    grace_seconds: float = 10.0
    # How many consecutive bad samples count as a regression. One sample can be
    # a scrape landing mid-restart; two in a row is the system telling you
    # something.
    consecutive_bad: int = 2

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.consecutive_bad < 1:
            raise ValueError("consecutive_bad must be at least 1")


@dataclass
class FollowUp:
    """Runs the commit → watch → revert loop and records every step.

    ``emit`` receives (kind, payload) for each trace event; wire it to the run
    ledger. ``sleep`` and ``now`` are injected so tests can drive a whole
    window without spending real time in it.
    """

    bake_in: BakeIn = field(default_factory=BakeIn)
    emit: Callable[[str, dict[str, Any]], None] | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(kind, payload)

    def run(
        self,
        action: str,
        probes: Sequence[HealthProbe],
        commit: Callable[[], bool],
        revert: Callable[[], None] | None = None,
        *,
        monotonic: bool = True,
    ) -> FollowUpVerdict:
        """Commit the change, watch it, and revert if a probe regresses.

        ``commit`` returns whether the change landed (the caller has already
        read it back through its contract). ``revert`` undoes it; passing None
        says the action has no inverse, in which case a regression is reported
        for a human instead of being silently tolerated.
        """
        if not probes:
            raise ValueError("a watch window with no probes proves nothing")

        # Baseline first: what was already broken before we touched anything.
        baseline: dict[str, bool] = {}
        for probe in probes:
            _, healthy = probe.sample()
            baseline[probe.name] = healthy

        verdict = FollowUpVerdict(
            action=action,
            outcome="not_committed",
            committed=False,
            baseline=baseline,
            window_seconds=self.bake_in.window_seconds,
        )

        if not commit():
            verdict.detail = "commit reported the change did not land; nothing to watch"
            self._record("bakein_opened", {"action": action, "committed": False})
            return verdict

        verdict.committed = True
        self._record(
            "remediation_committed",
            {"action": action, "monotonic": monotonic, "baseline": baseline},
        )
        self._record(
            "bakein_opened",
            {
                "action": action,
                "window_seconds": self.bake_in.window_seconds,
                "interval_seconds": self.bake_in.interval_seconds,
                "probes": [probe.name for probe in probes],
            },
        )

        if self.bake_in.grace_seconds > 0:
            self.sleep(self.bake_in.grace_seconds)

        bad_streak: dict[str, int] = {probe.name: 0 for probe in probes}
        regressed: list[str] = []
        elapsed = 0.0

        while elapsed < self.bake_in.window_seconds and not regressed:
            for probe in probes:
                reading, healthy = probe.sample()
                # Only a probe that was healthy before and is unhealthy now is
                # attributable to this change.
                is_regression = baseline.get(probe.name, False) and not healthy
                sample = Sample(
                    probe=probe.name,
                    at=self.now(),
                    reading=reading,
                    healthy=healthy,
                    regressed=is_regression,
                )
                verdict.samples.append(sample)
                self._record("bakein_sampled", sample.model_dump(mode="json"))

                bad_streak[probe.name] = bad_streak[probe.name] + 1 if is_regression else 0
                if bad_streak[probe.name] >= self.bake_in.consecutive_bad:
                    regressed.append(probe.name)

            if regressed:
                break
            self.sleep(self.bake_in.interval_seconds)
            elapsed += self.bake_in.interval_seconds

        if not regressed:
            verdict.outcome = "passed"
            verdict.detail = f"no probe regressed across {len(verdict.samples)} readings"
            self._record(
                "bakein_passed",
                {"action": action, "samples": len(verdict.samples), "elapsed_seconds": elapsed},
            )
            return verdict

        verdict.regressed_probes = regressed
        self._record(
            "bakein_regressed",
            {"action": action, "probes": regressed, "elapsed_seconds": elapsed},
        )

        if revert is None:
            verdict.outcome = "revert_unverified"
            verdict.detail = (
                f"{', '.join(regressed)} regressed and this action has no inverse; "
                "escalating rather than leaving it unreported"
            )
            self._record("revert_unverified", {"action": action, "reason": "no revert available"})
            return verdict

        try:
            revert()
        except Exception as error:  # noqa: BLE001 - the reason is reported, not swallowed
            verdict.outcome = "revert_unverified"
            verdict.detail = f"revert raised {type(error).__name__}: {error}"
            self._record(
                "revert_unverified",
                {"action": action, "reason": verdict.detail},
            )
            return verdict

        # A revert that returned without raising is still unproven. Read back.
        still_wrong: list[str] = []
        for probe in probes:
            reading, healthy = probe.sample()
            sample = Sample(probe=probe.name, at=self.now(), reading=reading, healthy=healthy)
            verdict.samples.append(sample)
            self._record("bakein_sampled", sample.model_dump(mode="json"))
            if baseline.get(probe.name, False) and not healthy:
                still_wrong.append(probe.name)

        if still_wrong:
            verdict.outcome = "revert_unverified"
            verdict.detail = (
                f"reverted, but {', '.join(still_wrong)} still reads worse than baseline"
            )
            self._record(
                "revert_unverified",
                {"action": action, "probes": still_wrong, "reason": "readback disagrees with baseline"},
            )
            return verdict

        verdict.outcome = "reverted"
        verdict.detail = f"{', '.join(regressed)} regressed; reverted and read back at baseline"
        self._record(
            "remediation_reverted",
            {"action": action, "probes": regressed, "verified": True},
        )
        return verdict
