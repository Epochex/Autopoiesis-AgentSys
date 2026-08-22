"""Shared gates for the tests that are grounded in this specific host.

Some tests here are deliberately not hermetic: blast radius, the investigation
eval and the live model paths assert against R450's actual interfaces, units and
captured dataset, because a blast-radius estimator verified only against mocks
verifies nothing. That is the right trade — but it means a GitHub runner, which
has none of that, cannot run them, and a suite that is permanently red on the
runner tells you nothing either.

So off-host they skip with a stated reason. The danger in that is obvious: a
gate that skips quietly would also hide a real regression on the host these
tests exist for — pull the cable out of eth2, or leave the gateway stopped, and
the suite would go green having verified nothing.

So the gate has two modes, chosen by whether this host carries the captured
dataset:

    dataset present  → this is the grounded host. A gate that does not open is
                       host drift, and the test FAILS saying what is missing.
    dataset absent   → a CI runner or a fresh clone. The test skips.

`AUTOPOIESIS_HOST_TESTS=skip` forces the lenient mode for anyone who has the
dataset but genuinely cannot provide the rest (a container without `ip`, say);
`=strict` forces the opposite. Neither is needed on R450 or on the runner.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATASET = REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "manifest.json"


def _strict() -> bool:
    """Should an unmet requirement fail rather than skip?"""
    override = os.getenv("AUTOPOIESIS_HOST_TESTS", "").strip().lower()
    if override == "strict":
        return True
    if override == "skip":
        return False
    return REAL_DATASET.exists()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which(argv[0]):
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _interface_exists(name: str) -> bool:
    done = _run(["ip", "-br", "link", "show", name])
    # `ip` exits 0 with empty stdout for a name it does not know, so presence is
    # decided by output, not by the exit code.
    return bool(done and done.stdout.strip())


def _unit_is_active(name: str) -> bool:
    done = _run(["systemctl", "is-active", name])
    return bool(done and done.stdout.strip() == "active")


def _interface_carrier(name: str) -> bool | None:
    """True/False if the link state is known, None if the interface is absent."""
    done = _run(["ip", "-br", "link", "show", name])
    line = done.stdout.strip() if done else ""
    if not line:
        return None
    # `ip -br link` prints e.g. "eth0  UP  aa:bb:.." / "eth0  DOWN  .."
    fields = line.split()
    return len(fields) > 1 and fields[1].upper() == "UP"


def _gate(met: bool, need: str) -> Callable[[Any], Any]:
    """Let the test run, skip it, or fail it — see the module docstring."""
    if met:
        return lambda test: test
    if not _strict():
        return pytest.mark.skipif(True, reason=f"needs {need}")

    def fail(test: Any) -> Any:
        @functools.wraps(test)
        def drifted(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail(
                f"host drift: this host carries the captured dataset, so it is the "
                f"host these tests exist to verify — but it no longer provides {need}. "
                f"Fix the host, or set AUTOPOIESIS_HOST_TESTS=skip to run without them."
            )
        return drifted

    return fail


def requires_host_interfaces(*names: str) -> Callable[[Any], Any]:
    missing = [n for n in names if not _interface_exists(n)]
    return _gate(not missing, f"these interfaces, missing here: {', '.join(missing) or '—'}")


def requires_active_unit(name: str) -> Callable[[Any], Any]:
    return _gate(_unit_is_active(name), f"{name} to be running")


def requires_idle_interface(name: str) -> Callable[[Any], Any]:
    """An interface that exists but carries nothing.

    Existence alone is not the property under test: a GitHub runner has an
    `eth0` too, but it is that host's live NIC, so the estimator correctly calls
    it blocked and the assertion for a measured zero fails for the right reason.
    """
    carrier = _interface_carrier(name)
    return _gate(
        carrier is False,
        f"{name} present but carrying nothing "
        + ("(absent here)" if carrier is None else f"({name} is up here)"),
    )


def _requires_real_dataset(test: Any) -> Any:
    # This one can only ever skip: absence of the dataset is what defines the
    # lenient mode, so it can never be host drift.
    return pytest.mark.skipif(
        not REAL_DATASET.exists(),
        reason="needs the captured R450 dataset (fixtures/real/manifest.json)",
    )(test)


requires_real_dataset = _requires_real_dataset
