"""The protected path must be refused identically by every layer that can name it.

These layers were written separately and drifted in both directions: the
read-only allowlist let `tailscale2` through while the remediation layer caught
it, and the remediation layer let `100.64.1.9/32` through while the allowlist
caught it. Each was the other's blind spot. The parity test below is the point
of this file — a new layer that forgets to use the shared predicate fails here.
"""

from __future__ import annotations

import pytest

from core.investigate.safe_exec import _touches_tailscale
from core.safety.tailscale import is_tailscale_target
from domains.network_rca.remediation import UnsafeTarget, assert_not_tailscale

ON_THE_PATH = [
    "tailscale0", "tailscale1", "tailscale2", "tailscale9", "tailscale",
    "tailscaled", "tailscaled.service", "TAILSCALE0",
    "100.64.1.9", "100.64.1.9/32", "100.127.255.254", "fd7a:115c:a1e0::1",
]

OFF_THE_PATH = [
    "eth0", "eth2", "eno1", "enp3s0", "autopoiesis-facts-ingest",
    "192.168.1.23", "10.42.0.1", "172.16.0.1", "100.63.255.255", "100.128.0.1",
    "", "lo", "docker0",
]


@pytest.mark.parametrize("target", ON_THE_PATH)
def test_protected_targets_are_recognised(target):
    assert is_tailscale_target(target) is True


@pytest.mark.parametrize("target", OFF_THE_PATH)
def test_ordinary_targets_are_not_swept_up(target):
    assert is_tailscale_target(target) is False


@pytest.mark.parametrize("target", ON_THE_PATH + OFF_THE_PATH)
def test_both_layers_agree_on_every_target(target):
    """Parity, not each layer's correctness — divergence is the failure mode."""
    allowlist_blocks = _touches_tailscale([target])
    try:
        assert_not_tailscale(target)
        remediation_blocks = False
    except UnsafeTarget:
        remediation_blocks = True
    assert allowlist_blocks == remediation_blocks, (
        f"{target!r}: allowlist={'block' if allowlist_blocks else 'allow'} "
        f"but remediation={'block' if remediation_blocks else 'allow'}"
    )


def test_the_cgnat_boundary_is_exact():
    """100.64.0.0/10 spans 100.64.x to 100.127.x, and nothing either side."""
    assert is_tailscale_target("100.64.0.0") is True
    assert is_tailscale_target("100.127.255.255") is True
    assert is_tailscale_target("100.63.255.255") is False
    assert is_tailscale_target("100.128.0.0") is False


def test_a_prefix_length_does_not_smuggle_an_address_past():
    assert is_tailscale_target("100.64.1.9/32") is True
    assert is_tailscale_target("100.64.0.0/10") is True


def test_a_future_interface_number_is_still_caught():
    """A guard that only knows today's interface number is not a guard."""
    assert is_tailscale_target("tailscale42") is True
