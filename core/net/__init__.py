"""Address helpers shared by every layer that reasons about the network."""

from core.net.addr import is_host_address, is_multicast_or_broadcast, is_private, segment_of

__all__ = ["is_host_address", "is_multicast_or_broadcast", "is_private", "segment_of"]
