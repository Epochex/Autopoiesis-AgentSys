"""Differential ownership probe: allowlisting, verdict logic, impact share.

Every test stubs the two functions that touch the network, so the suite never
opens a socket. What is under test is the reasoning -- in particular that a
single observed owner is reported as inconclusive rather than clean, because
this probe watches a short window and missing a handover is not evidence there
were none.
"""
from __future__ import annotations

import pytest

from domains.network_rca import ownership_probe
from domains.network_rca.ownership_probe import ALLOWED_PORTS, probe_address_ownership

SERVER = "50:9a:4c:87:29:b3"
SQUATTER = "d4:43:0e:1a:c5:88"


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(ownership_probe.time, "sleep", lambda _seconds: None)


def _wire(monkeypatch, owners: list[str], profiles: dict[str, set[int]]):
    """Owner sequence + which ports each owner serves."""
    sequence = iter(owners)
    current: dict[str, str] = {"mac": owners[0]}

    def fake_owner(_ip, timeout=3.0):
        current["mac"] = next(sequence, current["mac"])
        return current["mac"]

    def fake_tcp(_ip, port, timeout=1.0):
        return "open" if port in profiles.get(current["mac"], set()) else "refused"

    monkeypatch.setattr(ownership_probe, "_neighbour_mac", fake_owner)
    monkeypatch.setattr(ownership_probe, "_tcp_state", fake_tcp)


def test_two_owners_with_different_service_profiles_is_contested(monkeypatch, no_sleep):
    _wire(
        monkeypatch,
        [SQUATTER, SERVER, SQUATTER, SERVER],
        {SQUATTER: {22, 37777}, SERVER: {22, 10250}},
    )
    result = probe_address_ownership("192.168.1.23", ports=(22, 10250, 37777), samples=4, interval=0)

    assert result["verdict"] == "contested"
    assert set(result["owner_samples"]) == {SERVER, SQUATTER}
    assert result["service_profiles"][SQUATTER] == ["22", "37777"]
    assert result["service_profiles"][SERVER] == ["10250", "22"]
    assert result["read_only"] is True


def test_a_single_owner_is_inconclusive_not_clean(monkeypatch, no_sleep):
    """Not catching a handover in a short window is not evidence of no handover."""
    _wire(monkeypatch, [SERVER] * 6, {SERVER: {22, 10250}})
    result = probe_address_ownership("192.168.1.23", samples=6, interval=0)

    assert result["verdict"] == "inconclusive"
    assert "not proof the address is uncontested" in result["verdict_note"]


def test_two_owners_serving_identical_ports_is_not_called_contested(monkeypatch, no_sleep):
    """Same profile from both cannot separate the claimants, so it proves nothing."""
    _wire(monkeypatch, [SQUATTER, SERVER, SQUATTER, SERVER], {SQUATTER: {22}, SERVER: {22}})
    result = probe_address_ownership("192.168.1.23", ports=(22, 10250), samples=4, interval=0)
    assert result["verdict"] == "inconclusive"


def test_impact_share_counts_how_often_the_intended_service_is_unreachable(monkeypatch, no_sleep):
    # 3 samples on the squatter, 1 on the server -> kubelet is served 1/4 of the time
    _wire(
        monkeypatch,
        [SQUATTER, SQUATTER, SQUATTER, SERVER],
        {SQUATTER: {22, 37777}, SERVER: {22, 10250}},
    )
    result = probe_address_ownership("192.168.1.23", ports=(22, 10250, 37777), samples=4, interval=0)

    assert result["impact"]["10250"]["service"] == "kubelet"
    assert result["impact"]["10250"]["unreachable_share"] == 0.75
    assert result["impact"]["37777"]["unreachable_share"] == 0.25
    # port 22 answers from both claimants, so it is not an availability gap
    assert "22" not in result["impact"]


def test_ports_outside_the_allowlist_are_dropped(monkeypatch, no_sleep):
    _wire(monkeypatch, [SERVER] * 3, {SERVER: {22}})
    result = probe_address_ownership("192.168.1.23", ports=(22, 31337, 4444), samples=3, interval=0)
    assert list(result["ports"]) == ["22"]


def test_a_request_with_no_allowed_port_is_refused(monkeypatch, no_sleep):
    _wire(monkeypatch, [SERVER], {SERVER: set()})
    with pytest.raises(ValueError):
        probe_address_ownership("192.168.1.23", ports=(31337,), samples=3, interval=0)


def test_sample_count_and_port_count_are_bounded(monkeypatch, no_sleep):
    _wire(monkeypatch, [SERVER] * 200, {SERVER: {22}})
    result = probe_address_ownership(
        "192.168.1.23", ports=tuple(ALLOWED_PORTS)[:12], samples=9999, interval=0
    )
    assert result["samples"] == ownership_probe.MAX_SAMPLES
    assert len(result["ports"]) == ownership_probe.MAX_PORTS


def test_an_unresolvable_neighbour_entry_does_not_become_a_claimant(monkeypatch, no_sleep):
    monkeypatch.setattr(ownership_probe, "_neighbour_mac", lambda _ip, timeout=3.0: None)
    monkeypatch.setattr(ownership_probe, "_tcp_state", lambda _ip, _port, timeout=1.0: "timeout")
    result = probe_address_ownership("192.168.1.23", samples=3, interval=0)

    assert result["owner_samples"] == {"unresolved": 3}
    assert result["verdict"] == "inconclusive"
