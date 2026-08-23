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
    # A target must become healthy for the action to count as effective. A
    # guard protects collateral state and is judged against its pre-change
    # baseline. Keeping guard as the default preserves the generic probe
    # contract for callers that only want regression detection.
    role: Literal["target", "guard"] = "guard"
    # Critical guards such as the management plane can fail on the first bad
    # read instead of waiting for the window-wide bad-sample threshold.
    failure_threshold: int | None = None

    def sample(self) -> tuple[dict[str, Any], bool]:
        reading = dict(self.read())
        return reading, bool(self.healthy(reading))


class Sample(BaseModel):
    probe: str
    at: datetime
    reading: dict[str, Any]
    healthy: bool
    regressed: bool = False
    available: bool = True
    phase: Literal["baseline", "fast", "stability", "revert"] = "fast"


class FollowUpVerdict(BaseModel):
    """What the watch window concluded, with every reading it took."""

    action: str
    outcome: Outcome
    committed: bool
    baseline: dict[str, bool] = Field(default_factory=dict)
    samples: list[Sample] = Field(default_factory=list)
    regressed_probes: list[str] = Field(default_factory=list)
    window_seconds: float = 0.0
    stability_window_seconds: float = 0.0
    fast_samples: int = 0
    stability_samples: int = 0
    target_recovered: bool = False
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
    # The fast window catches immediate damage. The stability window is held
    # for its full duration so a brief recovery cannot close the incident.
    stability_window_seconds: float = 0.0
    success_consecutive: int = 2

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.consecutive_bad < 1:
            raise ValueError("consecutive_bad must be at least 1")
        if self.window_seconds < 0 or self.stability_window_seconds < 0:
            raise ValueError("watch window seconds cannot be negative")
        if self.grace_seconds < 0:
            raise ValueError("grace_seconds cannot be negative")
        if self.success_consecutive < 1:
            raise ValueError("success_consecutive must be at least 1")


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

    def _sample(self, probe: HealthProbe) -> tuple[dict[str, Any], bool, bool]:
        """Read one live probe and turn telemetry loss into an explicit state."""
        try:
            reading, healthy = probe.sample()
            return reading, healthy, True
        except Exception as error:  # noqa: BLE001 - the audit row carries the failure
            return {
                "error": type(error).__name__,
                "detail": str(error),
            }, False, False

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
        unavailable_baseline: list[str] = []
        for probe in probes:
            reading, healthy, available = self._sample(probe)
            baseline[probe.name] = healthy
            self._record(
                "bakein_sampled",
                Sample(
                    probe=probe.name,
                    at=self.now(),
                    reading=reading,
                    healthy=healthy,
                    available=available,
                    phase="baseline",
                ).model_dump(mode="json"),
            )
            if not available:
                unavailable_baseline.append(probe.name)

        verdict = FollowUpVerdict(
            action=action,
            outcome="not_committed",
            committed=False,
            baseline=baseline,
            window_seconds=self.bake_in.window_seconds,
            stability_window_seconds=self.bake_in.stability_window_seconds,
        )

        if unavailable_baseline:
            verdict.detail = (
                "live telemetry unavailable before commit: "
                + ", ".join(unavailable_baseline)
            )
            self._record(
                "bakein_opened",
                {"action": action, "committed": False, "reason": verdict.detail},
            )
            return verdict

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
                "stability_window_seconds": self.bake_in.stability_window_seconds,
                "interval_seconds": self.bake_in.interval_seconds,
                "probes": [probe.name for probe in probes],
            },
        )

        if self.bake_in.grace_seconds > 0:
            self.sleep(self.bake_in.grace_seconds)

        bad_streak: dict[str, int] = {probe.name: 0 for probe in probes}
        good_streak: dict[str, int] = {probe.name: 0 for probe in probes}
        regressed: list[str] = []
        elapsed = 0.0

        while elapsed < self.bake_in.window_seconds and not regressed:
            for probe in probes:
                reading, healthy, available = self._sample(probe)
                # Targets prove effectiveness; guards prove that the action did
                # not damage previously healthy collateral state. Missing live
                # telemetry is unsafe in either role.
                is_regression = (not available) or (
                    not healthy
                    and (probe.role == "target" or baseline.get(probe.name, False))
                )
                sample = Sample(
                    probe=probe.name,
                    at=self.now(),
                    reading=reading,
                    healthy=healthy,
                    regressed=is_regression,
                    available=available,
                    phase="fast",
                )
                verdict.samples.append(sample)
                verdict.fast_samples += 1
                self._record("bakein_sampled", sample.model_dump(mode="json"))

                bad_streak[probe.name] = bad_streak[probe.name] + 1 if is_regression else 0
                good_streak[probe.name] = good_streak[probe.name] + 1 if healthy else 0
                threshold = probe.failure_threshold or self.bake_in.consecutive_bad
                if bad_streak[probe.name] >= threshold:
                    regressed.append(probe.name)

            if regressed:
                break
            self.sleep(self.bake_in.interval_seconds)
            elapsed += self.bake_in.interval_seconds

        # A target that never reached the healthy state is an ineffective
        # action even when no individual bad streak crossed the threshold.
        if not regressed:
            unrecovered = [
                probe.name
                for probe in probes
                if probe.role == "target" and good_streak[probe.name] < 1
            ]
            regressed.extend(unrecovered)

        # Hold a separate, longer stability window. Successful samples do not
        # shorten it; this catches a component that briefly returns and dies
        # again after the fast rollback window.
        stability_elapsed = 0.0
        if not regressed and self.bake_in.stability_window_seconds > 0:
            self._record(
                "stability_opened",
                {
                    "action": action,
                    "window_seconds": self.bake_in.stability_window_seconds,
                    "success_consecutive": self.bake_in.success_consecutive,
                },
            )
            while stability_elapsed < self.bake_in.stability_window_seconds and not regressed:
                for probe in probes:
                    reading, healthy, available = self._sample(probe)
                    is_regression = (not available) or (
                        not healthy
                        and (probe.role == "target" or baseline.get(probe.name, False))
                    )
                    sample = Sample(
                        probe=probe.name,
                        at=self.now(),
                        reading=reading,
                        healthy=healthy,
                        regressed=is_regression,
                        available=available,
                        phase="stability",
                    )
                    verdict.samples.append(sample)
                    verdict.stability_samples += 1
                    self._record("bakein_sampled", sample.model_dump(mode="json"))
                    bad_streak[probe.name] = bad_streak[probe.name] + 1 if is_regression else 0
                    good_streak[probe.name] = good_streak[probe.name] + 1 if healthy else 0
                    threshold = probe.failure_threshold or self.bake_in.consecutive_bad
                    if bad_streak[probe.name] >= threshold:
                        regressed.append(probe.name)
                if regressed:
                    break
                self.sleep(self.bake_in.interval_seconds)
                stability_elapsed += self.bake_in.interval_seconds

            if not regressed:
                unstable = [
                    probe.name
                    for probe in probes
                    if (
                        probe.role == "target"
                        or baseline.get(probe.name, False)
                    )
                    and good_streak[probe.name] < self.bake_in.success_consecutive
                ]
                regressed.extend(unstable)

        if not regressed:
            verdict.outcome = "passed"
            targets = [probe.name for probe in probes if probe.role == "target"]
            verdict.target_recovered = all(good_streak[name] >= 1 for name in targets)
            verdict.detail = (
                f"targets recovered and guards held across {len(verdict.samples)} live readings"
                if targets
                else f"no probe regressed across {len(verdict.samples)} live readings"
            )
            self._record(
                "bakein_passed",
                {
                    "action": action,
                    "samples": len(verdict.samples),
                    "fast_elapsed_seconds": elapsed,
                    "stability_elapsed_seconds": stability_elapsed,
                },
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
            reading, healthy, available = self._sample(probe)
            sample = Sample(
                probe=probe.name,
                at=self.now(),
                reading=reading,
                healthy=healthy,
                available=available,
                phase="revert",
            )
            verdict.samples.append(sample)
            self._record("bakein_sampled", sample.model_dump(mode="json"))
            if not available or (baseline.get(probe.name, False) and not healthy):
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
