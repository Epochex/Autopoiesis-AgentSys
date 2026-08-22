"""Blast radius has to be measured, and has to say so when it cannot be.

The failure mode this guards is a confident zero: an estimate that reports "no
impact" because a probe returned nothing, rather than because nothing would be
affected. Every branch below distinguishes "measured as empty" from "could not
measure".
"""

from __future__ import annotations

from frontend.gateway.app.blast_radius import estimate


def test_the_protected_path_is_refused_before_anything_is_measured():
    result = estimate("bounce_interface", "tailscale0")
    assert result["scope"] == "refused"
    assert result["reversible"] is False
    assert "唯一通道" in result["summary"]


def test_a_carrying_interface_reports_what_rides_on_it():
    """eth2 holds this host's address and default route."""
    result = estimate("bounce_interface", "eth2")
    assert result["scope"] == "blocked"
    assert result["measured"]["carrier"] is True
    assert result["measured"]["addresses"], "an address must be named, not implied"
    assert result["measured"]["routes"] >= 1


def test_an_idle_interface_reports_a_measured_zero():
    result = estimate("bounce_interface", "eth0")
    assert result["scope"] == "single-nic"
    assert result["measured"]["carrier"] is False
    assert result["measured"]["addresses"] == []


def test_a_missing_interface_is_not_reported_as_harmless():
    """`ip` writes its complaint to stderr and exits non-zero — a missing NIC
    has more output than a present one, so output length cannot decide this."""
    result = estimate("bounce_interface", "eth9")
    assert result["scope"] == "none"
    assert result["measured"]["exists"] is False


def test_a_running_unit_is_reported_as_blocked():
    result = estimate("restart_unit", "netops-ops-console-backend")
    assert result["scope"] == "blocked"
    assert result["measured"]["state"] == "active"


def test_port_closure_admits_what_it_cannot_measure():
    """Distinct sources over a week needs the flow store; until then, unknown."""
    result = estimate("close_port", "192.168.1.46", port="23")
    assert result["scope"] == "service-wide"
    assert result["reversible"] is False
    assert result["measured"]["distinct_sources"] is None
    assert "按“有人在用”对待" in result["summary"]


def test_an_unknown_action_says_so_rather_than_guessing():
    result = estimate("reformat_everything", "eth0")
    assert result["scope"] == "unknown"
    assert result["reversible"] is None
