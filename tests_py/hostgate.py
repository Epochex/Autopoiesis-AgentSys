"""Shared gates for the tests that are grounded in this specific host.

Some tests here are deliberately not hermetic: blast radius, the investigation
eval and the live model paths assert against R450's actual interfaces, units and
captured dataset, because a blast-radius estimator verified only against mocks
verifies nothing. That is the right trade — but it means a GitHub runner, which
has none of that, cannot run them, and a suite that is permanently red on the
runner tells you nothing either.

So they skip with a stated reason instead of failing. The gates below say what
each one needs, so a skip is a fact about the environment rather than a mystery.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATASET = REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "manifest.json"


def _interface_exists(name: str) -> bool:
    if not shutil.which("ip"):
        return False
    try:
        done = subprocess.run(
            ["ip", "-br", "link", "show", name],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # `ip` exits 0 with empty stdout for a name it does not know, so presence is
    # decided by output, not by the exit code.
    return bool(done.stdout.strip())


def _unit_is_active(name: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        done = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "active"


def _interface_carrier(name: str) -> bool | None:
    """True/False if the link state is known, None if the interface is absent."""
    if not shutil.which("ip"):
        return None
    try:
        done = subprocess.run(
            ["ip", "-br", "link", "show", name],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = done.stdout.strip()
    if not line:
        return None
    # `ip -br link` prints e.g. "eth0  UP  aa:bb:.." / "eth0  DOWN  .."
    fields = line.split()
    return len(fields) > 1 and fields[1].upper() == "UP"


def requires_host_interfaces(*names: str) -> pytest.MarkDecorator:
    missing = [n for n in names if not _interface_exists(n)]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"needs this host's interfaces, absent here: {', '.join(missing)}",
    )


def requires_active_unit(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not _unit_is_active(name),
        reason=f"needs {name} running on this host",
    )


def requires_idle_interface(name: str) -> pytest.MarkDecorator:
    """An interface that exists but carries nothing.

    Existence alone is not the property under test: a GitHub runner has an
    `eth0` too, but it is that host's live NIC, so the estimator correctly calls
    it blocked and the assertion for a measured zero fails for the right reason.
    """
    carrier = _interface_carrier(name)
    return pytest.mark.skipif(
        carrier is not False,
        reason=(f"needs {name} present but carrying nothing; "
                + ("absent here" if carrier is None else f"{name} is up on this host")),
    )


requires_real_dataset = pytest.mark.skipif(
    not REAL_DATASET.exists(),
    reason="needs the captured R450 dataset (fixtures/real/manifest.json), gated to local runs",
)
