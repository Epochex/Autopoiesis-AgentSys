"""One address predicate, and proof the three call sites now agree.

The three hand-rolled versions disagreed on 172.16/12 and on the middle of the
multicast block, and all three called loopback and link-local external. None of
it changed a result on this network — the gateway corpus only carries
192.168.x — which is why it was cheap to fix and would have been expensive to
discover later.
"""

from __future__ import annotations

import pytest

from core.net.addr import is_host_address, is_multicast_or_broadcast, is_private, segment_of
from domains.network_rca.build_device_graph import _internal, _is_bcast as graph_bcast
from domains.network_rca.environment import _is_private as env_private
from frontend.gateway.app.device_profile import _is_bcast as profile_bcast, _is_private as profile_private

ADDRESSES = [
    "192.168.1.23", "192.168.16.4", "10.42.0.1", "172.16.0.1", "172.18.0.5",
    "172.20.5.5", "172.31.9.9", "127.0.0.1", "169.254.1.1", "100.64.1.9",
    "8.8.8.8", "1.1.1.1", "not-an-ip", "", "192.168.1",
]


@pytest.mark.parametrize("address", ADDRESSES)
def test_all_three_private_checks_agree(address):
    """Parity is the property; each one being right follows from sharing code."""
    verdicts = {_internal(address), env_private(address), profile_private(address)}
    assert len(verdicts) == 1, f"{address!r} split the three call sites: {verdicts}"


@pytest.mark.parametrize(
    "address", ["225.1.1.1", "231.5.5.5", "238.0.0.1", "224.0.0.1", "239.255.255.250",
                "192.168.1.255", "255.255.255.255", "192.168.1.23", "not-an-ip"]
)
def test_both_broadcast_checks_agree(address):
    assert graph_bcast(address) == profile_bcast(address)


def test_the_whole_rfc1918_space_is_private_not_just_the_bits_we_remembered():
    for address in ["10.0.0.1", "172.16.0.1", "172.20.5.5", "172.31.255.254", "192.168.0.1"]:
        assert is_private(address) is True
    assert is_private("172.15.0.1") is False
    assert is_private("172.32.0.1") is False


def test_loopback_and_link_local_are_private():
    """All three old versions called these external."""
    assert is_private("127.0.0.1") is True
    assert is_private("169.254.1.1") is True


def test_unparseable_input_is_not_quietly_internal():
    for value in ["", "hostname", "192.168.1", "192.168.1.999", None]:
        assert is_private(str(value)) is False


def test_the_middle_of_the_multicast_block_is_not_skipped():
    """One old version matched only 224./239./255., missing 225 through 238."""
    for address in ["224.0.0.1", "225.1.1.1", "231.5.5.5", "238.0.0.1", "239.255.255.250"]:
        assert is_multicast_or_broadcast(address) is True
    assert is_multicast_or_broadcast("192.168.1.23") is False


def test_a_host_address_is_private_and_not_a_group():
    assert is_host_address("192.168.1.23") is True
    assert is_host_address("239.255.255.250") is False
    assert is_host_address("8.8.8.8") is False


def test_segment_of_returns_a_cidr_or_nothing():
    assert segment_of("192.168.1.23") == "192.168.1.0/24"
    assert segment_of("10.42.0.1") == "10.42.0.0/24"
    assert segment_of("nonsense") == ""


def test_this_networks_real_addresses_are_unaffected():
    """The change is free here: every address in the corpus reads the same."""
    for address in ["192.168.1.1", "192.168.1.23", "192.168.1.27", "192.168.1.46", "192.168.16.4"]:
        assert _internal(address) is True
        assert env_private(address) is True
        assert profile_private(address) is True
