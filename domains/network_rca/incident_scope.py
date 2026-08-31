"""Derive a bounded investigation scope from landed incident facts.

The scope is a business input to every later component.  Public actors are kept
as evidence subjects, while only assets owned by this deployment are eligible
probe or action targets.  Time bounds come from the detector window and source
timestamps.  Topology is used to name the affected segment and interface; it is
never used to invent an endpoint that the event did not contain.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ip(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(value or ""))
    except ValueError:
        return None


def _private(value: Any) -> bool:
    address = _ip(value)
    return bool(address and (address.is_private or address.is_loopback or address.is_link_local))


def _public(value: Any) -> bool:
    address = _ip(value)
    return bool(
        address
        and not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        )
    )


def _positive_seconds(value: Any) -> int | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds <= 86_400 else None


def _network_for(value: str, topology: Mapping[str, Any]) -> tuple[str, str] | None:
    address = _ip(value)
    if address is None:
        return None
    for subnet in topology.get("subnets") or ():
        try:
            network = ipaddress.ip_network(str(subnet.get("cidr") or ""), strict=False)
        except ValueError:
            continue
        if address in network:
            return str(network), str(subnet.get("intf") or "")
    return None


@dataclass(frozen=True, slots=True)
class IncidentScope:
    primary_asset: str | None
    managed_assets: tuple[str, ...]
    external_actors: tuple[str, ...]
    incident_start: str
    incident_end: str
    fault_domain: str
    quality: str
    basis: tuple[str, ...]
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.primary_asset,
            "asset_ids": list(self.managed_assets),
            "external_actors": list(self.external_actors),
            "incident_start": self.incident_start,
            "incident_end": self.incident_end,
            "fault_domain": self.fault_domain,
            "scope_quality": self.quality,
            "scope_basis": list(self.basis),
            "scope_missing": list(self.missing),
        }


def derive_incident_scope(
    *,
    subject: str | None,
    service: str | None,
    first_seen_at: str,
    last_seen_at: str,
    facts: Mapping[str, Any],
    managed_gateway: str,
    fault_family: str | None = None,
    topology: Mapping[str, Any] | None = None,
    textual_identifiers: Sequence[str] = (),
) -> IncidentScope:
    """Return probe/action scope without treating every mentioned IP as managed."""

    topology = topology or {}
    first = _utc(first_seen_at)
    last = _utc(last_seen_at)
    observed_raw = str(facts.get("observedAt") or "").strip()
    try:
        observed = _utc(observed_raw) if observed_raw else last
    except ValueError:
        observed = last
    window_seconds = _positive_seconds(facts.get("windowSeconds"))
    if window_seconds is not None:
        first = min(first, observed - timedelta(seconds=window_seconds))
        last = max(last, observed)

    # Add a small query tolerance for collector and device clock skew.  The
    # tolerance scales with the detector window and is bounded at two minutes.
    tolerance = min(120, max(5, (window_seconds or 60) // 10))
    first -= timedelta(seconds=tolerance)
    last += timedelta(seconds=tolerance)

    source = str(facts.get("sourceIp") or "").strip()
    destination = str(facts.get("destinationIp") or "").strip()
    ingress = str(facts.get("sourceInterface") or "").strip()
    egress = str(facts.get("destinationInterface") or "").strip()
    policy_id = str(facts.get("policyId") or "").strip()
    subtype = str(facts.get("trafficSubtype") or "").casefold()
    policy_type = str(facts.get("policyType") or "").casefold()
    # FortiOS uses a distinct policy type for IPv6 local traffic.  Both forms
    # terminate on the managed firewall, including link-local multicast such as
    # SSDP/WS-Discovery.  Treating ``local-in-policy6`` as an ordinary forwarded
    # flow loses the only managed target even though the ingress interface and
    # policy class already locate the fault domain exactly.
    local_in = subtype == "local" and policy_type in {
        "local-in-policy",
        "local-in-policy6",
    }

    managed: list[str] = []
    external: list[str] = []
    basis: list[str] = []
    missing: list[str] = []

    def add_managed(value: str, reason: str) -> None:
        if value and value not in managed:
            managed.append(value)
            basis.append(reason)

    if _public(source):
        external.append(source)
    if _public(destination) and not local_in:
        external.append(destination)

    gateway_policy_scope = bool(
        fault_family == "fam-policy-reachability"
        and (
            local_in
            or _public(subject)
            or str(subject or "").casefold() in {"r230", "fortigate", "gateway"}
        )
    )
    management_auth_scope = fault_family == "fam-management-auth"
    if local_in or gateway_policy_scope or management_auth_scope:
        add_managed(
            managed_gateway,
            (
                "authentication event belongs to the managed gateway"
                if management_auth_scope else
                "local-in traffic terminates on the managed gateway"
                if local_in
                else "policy alert is inspected on the managed gateway"
            ),
        )
        fault_domain = (
            f"gateway-management-plane:{managed_gateway}"
            if management_auth_scope else
            f"gateway-control-plane:{managed_gateway}:{ingress or 'unknown-interface'}"
            if local_in
            else f"gateway-policy:{managed_gateway}:{ingress or 'unknown-interface'}"
        )
        if local_in and not ingress:
            missing.append("sourceInterface")
    else:
        for value, role in ((source, "sourceIp"), (destination, "destinationIp")):
            if _private(value):
                located = _network_for(value, topology)
                if located is not None:
                    add_managed(value, f"{role} belongs to observed managed segment {located[0]}")
        subject_text = str(subject or "").strip()
        if subject_text.endswith((".service", ".target", ".socket", ".timer")):
            add_managed(subject_text, "detector named an exact managed host unit")
        elif subject_text and _private(subject_text) and _network_for(subject_text, topology):
            add_managed(subject_text, "detector subject belongs to an observed managed segment")
        elif subject_text and not _ip(subject_text):
            # Interface and local host identifiers are valid exact targets. Free
            # prose is deliberately excluded by this narrow character contract.
            if all(char.isalnum() or char in "_.:@-" for char in subject_text):
                add_managed(subject_text, "detector named an exact local asset identifier")

        src_segment = _network_for(source, topology) if source else None
        dst_segment = _network_for(destination, topology) if destination else None
        if ingress or egress:
            fault_domain = f"forwarding-path:{ingress or '?'}->{egress or '?'}"
            if policy_id:
                fault_domain += f":policy-{policy_id}"
        elif src_segment and dst_segment:
            fault_domain = f"segment-path:{src_segment[0]}->{dst_segment[0]}"
        elif managed:
            fault_domain = f"asset:{managed[0]}"
        else:
            fault_domain = "unresolved"

    # Text extraction is a last-resort locator only for private addresses that
    # are present in the observed topology. It never turns a public mention into
    # a probe target.
    for value in textual_identifiers:
        located = _network_for(value, topology)
        address = _ip(value)
        names_host = False
        if located is not None and address is not None:
            network = ipaddress.ip_network(located[0], strict=False)
            names_host = address not in {network.network_address, network.broadcast_address}
        if _private(value) and located and names_host:
            add_managed(value, "private identifier in source record belongs to observed topology")

    if not managed:
        missing.append("managedAsset")
    if not facts:
        missing.append("incidentFacts")
    quality = "exact" if managed and facts and fault_domain != "unresolved" else (
        "partial" if managed else "unresolved"
    )
    primary = managed[0] if managed else None
    return IncidentScope(
        primary_asset=primary,
        managed_assets=tuple(managed),
        external_actors=tuple(dict.fromkeys(external)),
        incident_start=first.isoformat(),
        incident_end=last.isoformat(),
        fault_domain=fault_domain,
        quality=quality,
        basis=tuple(dict.fromkeys(basis)),
        missing=tuple(dict.fromkeys(missing)),
    )


__all__ = ["IncidentScope", "derive_incident_scope"]
