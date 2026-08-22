"""The one predicate that decides whether something is on the protected path.

Tailscale carries the only route into this network from outside. Cut it and the
operator is locked out with no way back in, so every layer that can name a
target — the read-only command allowlist, the remediation actions, anything
added later — has to refuse the same set.

It used to be written twice, and the two copies disagreed in both directions:
the allowlist matched only ``tailscale0``/``tailscale1``/``tailscaled`` as
substrings and let ``tailscale2`` through, while the remediation layer used a
``startswith`` and caught it; the allowlist stripped a ``/prefix`` before
testing an address and the remediation layer did not, so ``100.64.1.9/32``
passed there. Each copy was the other's blind spot, which is the failure mode a
shared safety predicate exists to prevent.

The rule here is deliberately broad. A false positive costs one refused
diagnostic; a false negative costs the way back into the network.
"""

from __future__ import annotations

import ipaddress

# The CGNAT block Tailscale assigns from.
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Interface and unit names. Matched by prefix rather than exact value: an
# interface may be tailscale0 today and tailscale1 after a reconfigure, and a
# guard that only knows today's number is not a guard.
TAILSCALE_PREFIX = "tailscale"

# Systemd units and binaries that manage the tunnel.
TAILSCALE_UNITS = frozenset({"tailscaled", "tailscaled.service", "tailscale"})


def is_tailscale_target(token: str) -> bool:
    """Whether this token names, or sits on, the protected path.

    Accepts an interface name, a unit name, a bare address, or an address with
    a prefix length. Anything unrecognised is not on the path.
    """
    name = (token or "").strip().lower()
    if not name:
        return False
    if name.startswith(TAILSCALE_PREFIX) or name in TAILSCALE_UNITS:
        return True

    # An address may arrive as 100.64.1.9 or 100.64.1.9/32; both are the path.
    candidate = name.split("/", 1)[0].strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.version != 4:
        # Tailscale also hands out ULAs under fd7a:115c:a1e0::/48.
        return candidate.startswith("fd7a:115c:a1e0")
    return address in TAILSCALE_CGNAT


def any_tailscale_target(tokens) -> bool:
    """Whether any token in an argv-like sequence is on the protected path."""
    return any(is_tailscale_target(str(token)) for token in tokens)


def refusal(token: str) -> str:
    """The reason to report, phrased so the operator knows why it is absolute."""
    return (
        f"{token} 在 Tailscale 路径上。这是从外面进入这个内网的唯一一条路，"
        "任何自动或人工的修复动作都不许碰它——断了就再也连不进来。"
    )
