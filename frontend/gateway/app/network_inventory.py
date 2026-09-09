"""Device ownership and interface-scoped traffic for the production workspace."""
from __future__ import annotations

import ipaddress
from typing import Any, Callable

from .history import _CH_DB, _CH_TABLE, _q


def managed_devices(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    identities = snapshot.get("identity", {}).get("items") or []
    identity = identities[0] if identities else {}
    serial = identity.get("serial")
    # Without an attested identity, do not manufacture a managed device.
    if not serial:
        return []
    device_id = f"fortigate:{serial}"
    interfaces = []
    for raw in snapshot.get("interfaces", {}).get("items") or []:
        name = str(raw.get("name") or "")
        if not name:
            continue
        addresses = []
        for value in [raw.get("ip"), *(raw.get("secondary_ips") or [])]:
            try:
                address = ipaddress.ip_interface(str(value).strip().replace(" ", "/"))
            except ValueError:
                continue
            if address.ip.is_unspecified:
                continue
            addresses.append({"ip": str(address.ip), "cidr": str(address),
                              "network": str(address.network), "public": address.ip.is_global})
        vdom = str(raw.get("vdom") or "root")
        interfaces.append({
            "id": f"{device_id}:{vdom}:{name}", "name": name, "vdom": vdom,
            "role": raw.get("role") or "undefined", "status": raw.get("status") or "unknown",
            "kind": raw.get("type"), "parent": raw.get("parent_interface"),
            "members": raw.get("members") or [], "vlanId": raw.get("vlan_id"),
            "addresses": addresses,
        })
    interfaces.sort(key=lambda row: (row["role"] != "wan", not bool(row["addresses"]), row["name"]))
    return [{"id": device_id, "serial": serial,
             "name": identity.get("hostname") or f"FortiGate {identity.get('model') or ''}".strip(),
             "model": identity.get("model"), "interfaces": interfaces,
             "observedAt": snapshot.get("interfaces", {}).get("fetched_at") or snapshot.get("fetched_at")}]


def owned_addresses(devices: list[dict[str, Any]]) -> set[str]:
    return {address["ip"] for device in devices for interface in device["interfaces"]
            for address in interface["addresses"]}


def interface_traffic(device: dict[str, Any], interface: dict[str, Any], days: int,
                      query: Callable = _q) -> dict[str, Any]:
    """Count each session once; rx/tx are relative to the selected interface.

    Source-interface forward packets enter the appliance; destination-interface
    forward packets leave it. The reverse bytes travel in the opposite direction.
    No summing across interfaces (which would double-count forwarded sessions).
    """
    days = max(1, min(int(days), 7))
    def literal(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    name = literal(interface["name"])
    # This configured fact store belongs to the single connected FortiGate.
    # It does not retain VDOM/serial columns, so reject ambiguous interface names.
    if sum(i["name"] == interface["name"] for i in device["interfaces"]) != 1:
        raise ValueError("Flow store cannot distinguish duplicate interface names across VDOMs")
    where = (
        f"type='traffic' AND event_ts >= now() - INTERVAL {days} DAY "
        f"AND (srcintf={name} OR dstintf={name}) "
        "AND ((sentbyte+rcvdbyte)>0 OR action NOT IN ('deny','blocked','reject'))"
    )
    rows = query(
        "SELECT srcip, dstip, srcintf, dstintf, subtype, "
        "argMax(dstcountry,event_ts) AS country, groupUniqArray(service) AS services, "
        "count() AS records, sum(sentbyte) AS sent, sum(rcvdbyte) AS received, "
        "min(event_ts) AS firstSeenAt, max(event_ts) AS lastSeenAt "
        f"FROM {_CH_DB}.{_CH_TABLE} WHERE {where} "
        "GROUP BY srcip,dstip,srcintf,dstintf,subtype ORDER BY lastSeenAt DESC"
    )
    totals = {"rx": 0, "tx": 0, "records": 0, "forwardBytes": 0, "localBytes": 0}
    for row in rows:
        sent, received = int(row.pop("sent") or 0), int(row.pop("received") or 0)
        # Hairpin: both legs use the same interface, so both directions cross it.
        ingress, egress = row["srcintf"] == interface["name"], row["dstintf"] == interface["name"]
        row["rx"] = (sent if ingress else 0) + (received if egress else 0)
        row["tx"] = (received if ingress else 0) + (sent if egress else 0)
        row["records"] = int(row["records"])
        for key in ("rx", "tx", "records"):
            totals[key] += row[key]
        category = "forwardBytes" if row["subtype"] == "forward" else "localBytes"
        totals[category] += row["rx"] + row["tx"]
    return {"ok": True, "deviceId": device["id"], "interfaceId": interface["id"],
            "days": days, "totals": totals, "records": rows,
            "firstSeenAt": min((r["firstSeenAt"] for r in rows), default=None),
            "lastSeenAt": max((r["lastSeenAt"] for r in rows), default=None)}
