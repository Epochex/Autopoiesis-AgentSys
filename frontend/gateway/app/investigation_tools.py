"""Read-only network evidence adapters used by a live investigation."""

from __future__ import annotations

import ipaddress
from typing import Any, Callable

from domains.network_rca.fortigate_api import FortiGateReadonlyAPI


def _is_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def collect_fortigate_context(
    subject: str | None,
    *,
    api_factory: Callable[..., FortiGateReadonlyAPI] = FortiGateReadonlyAPI.from_env,
    device_status_reader: Callable[[str, str], dict[str, Any] | None] | None = None,
    live_flow_reader: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Return a bounded router-side snapshot without exposing credentials.

    The FortiGate has much more state than a model prompt should receive.  This
    adapter keeps only the fields that can change an investigation: the target's
    current identity and sessions, interface/VLAN state, policy direction/action,
    and the latest configuration-revision metadata.
    """
    if device_status_reader is None or live_flow_reader is None:
        from .live_identity import device_status, live_flows

        device_status_reader = device_status_reader or device_status
        live_flow_reader = live_flow_reader or live_flows

    api = api_factory(verify_tls=False, timeout=5.0, retries=1)
    snapshot = api.fetch_all()
    devices = snapshot["devices"].get("items") or []
    target_devices = [
        item
        for item in devices
        if subject and subject in {str(item.get("ip") or ""), str(item.get("mac") or "")}
    ][:8]
    result: dict[str, Any] = {
        "fetched_at": snapshot["fetched_at"],
        "degraded": bool(snapshot.get("degraded")),
        "availability": {
            key: {
                "available": bool(snapshot[key].get("available")),
                "degraded": bool(snapshot[key].get("degraded")),
                "missing": list(snapshot[key].get("missing") or ()),
            }
            for key in ("interfaces", "devices", "policies", "changes")
        },
        "target_devices": target_devices,
        "interfaces": list(snapshot["interfaces"].get("items") or ())[:64],
        "policies": list(snapshot["policies"].get("items") or ())[:128],
        "changes": list(snapshot["changes"].get("items") or ())[:20],
        "inventory_count": len(devices),
    }
    if _is_ip(subject):
        result["target_status"] = device_status_reader(subject, "zh")
        result["target_flows"] = live_flow_reader(subject)
    return result


__all__ = ["collect_fortigate_context"]
