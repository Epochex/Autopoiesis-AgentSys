"""Address predicates, written once.

Three hand-rolled versions of "is this a private address" had drifted into
disagreement: 172.18.0.5 read as external in the graph builder and internal in
the other two, 172.20–172.31 read as external in two of three, and all three
called 127.0.0.1 and 169.254.x external. Each was a prefix-tuple written from
memory rather than from the RFC.

None of that changed a result on this network, because the gateway corpus only
ever carries 192.168.x — the divergent ranges appear zero times. That is
exactly why it was worth fixing now: today it is free, and the first time a
172.16/12 segment shows up the three views would have silently disagreed about
whether its devices exist.

``ipaddress`` already knows the answer. These wrappers exist so there is one
import to reach for, not to reimplement it.
"""

from __future__ import annotations

import ipaddress


def is_private(value: str) -> bool:
    """RFC1918, loopback and link-local — anything not routed on the internet.

    Unparseable input is not private: a hostname or a truncated field should
    not be quietly treated as an internal asset.
    """
    try:
        return ipaddress.ip_address(str(value).strip()).is_private
    except ValueError:
        return False


def is_multicast_or_broadcast(value: str) -> bool:
    """Addresses that name a group rather than a host.

    The two previous versions disagreed on the middle of the multicast block:
    one matched only 224./239./255. as prefixes, the other the whole 224–239
    first octet, so 225.x, 231.x and 238.x were filtered from one device graph
    and kept in the other.
    """
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    return address.is_multicast or address == ipaddress.ip_address("255.255.255.255")


def is_host_address(value: str) -> bool:
    """A private address that names one machine — what a device graph wants."""
    return is_private(value) and not is_multicast_or_broadcast(value)


def segment_of(value: str) -> str:
    """The /24 an address sits in, as a CIDR string. Empty when unparseable."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return ""
    if address.version != 4:
        return ""
    octets = str(address).split(".")
    return ".".join(octets[:3]) + ".0/24"
