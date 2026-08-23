"""FortiGate REST 客户端的只读边界、降级语义和字段归一化。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import inspect
import json
import os
from typing import Any

import pytest

import domains.network_rca.fortigate_api as fortigate_api
from domains.network_rca.fortigate_api import FortiGateReadonlyAPI


NOW = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)


class FakeHttpClient:
    def __init__(self, replies: Mapping[str, Any]):
        self.replies = dict(replies)
        self.calls: list[dict[str, Any]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
                "verify_tls": verify_tls,
            }
        )
        path = next(key for key in self.replies if key in url)
        reply = self.replies[path]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _api(fake: FakeHttpClient, **options: Any) -> FortiGateReadonlyAPI:
    return FortiGateReadonlyAPI(
        "https://192.168.1.1",
        "readonly-user",
        "secret-value",
        http_client=fake,
        clock=lambda: NOW,
        sleeper=lambda _: None,
        **options,
    )


def test_interfaces_supply_topology_fields_and_keep_tailscale_visible():
    fake = FakeHttpClient(
        {
            "/api/v2/cmdb/system/interface": {
                "status": "success",
                "results": [
                    {
                        "name": "vlan-office",
                        "status": "up",
                        "ip": ["192.168.16.1", "255.255.255.0"],
                        "role": "lan",
                        "vlanid": 16,
                        "interface": "port5",
                    },
                    {
                        "name": "tailscale0",
                        "status": "up",
                        "ip": "100.64.0.1/10",
                        "role": "lan",
                    },
                ],
            }
        }
    )

    result = _api(fake).fetch_interfaces()

    assert result == {
        "kind": "interfaces",
        "purpose": "网络拓扑：说明设备挂在哪个接口及其 VLAN 归属",
        "fetched_at": "2026-08-23T16:00:00Z",
        "available": True,
        "degraded": False,
        "items": [
            {
                "name": "vlan-office",
                "status": "up",
                "ip": "192.168.16.1/255.255.255.0",
                "role": "lan",
                "vlan_id": 16,
                "parent_interface": "port5",
                "missing_fields": [],
            },
            {
                "name": "tailscale0",
                "status": "up",
                "ip": "100.64.0.1/10",
                "role": "lan",
                "vlan_id": None,
                "parent_interface": None,
                "missing_fields": ["vlan_id"],
            },
        ],
        "missing": [],
    }
    assert fake.calls[0]["verify_tls"] is True
    assert fake.calls[0]["timeout"] == 5.0


def test_dhcp_and_known_devices_are_merged_for_ip_identity():
    fake = FakeHttpClient(
        {
            "/api/v2/monitor/system/dhcp": {
                "status": "success",
                "results": {
                    "root": [
                        {"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.16.96", "hostname": "cam-96"},
                        {"mac": "AA:BB:CC:DD:EE:02", "ip": "192.168.1.20"},
                    ]
                },
            },
            "/api/v2/monitor/user/device/query": {
                "status": "success",
                "results": {
                    "devices": [
                        {
                            "mac_address": "AA:BB:CC:DD:EE:02",
                            "ip_address": "192.168.1.20",
                            "device_name": "printer-20",
                        }
                    ]
                },
            },
        }
    )

    result = _api(fake).fetch_devices()

    assert result["purpose"].startswith("设备识别")
    assert result["available"] is True
    assert result["degraded"] is False
    assert result["missing"] == []
    assert result["items"] == [
        {
            "mac": "AA:BB:CC:DD:EE:01",
            "ip": "192.168.16.96",
            "hostname": "cam-96",
            "sources": ["dhcp_lease"],
            "missing_fields": [],
        },
        {
            "mac": "AA:BB:CC:DD:EE:02",
            "ip": "192.168.1.20",
            "hostname": "printer-20",
            "sources": ["dhcp_lease", "known_device"],
            "missing_fields": [],
        },
    ]


def test_device_read_reports_one_missing_source_without_discarding_the_other():
    fake = FakeHttpClient(
        {
            "/api/v2/monitor/system/dhcp": PermissionError("monitor permission denied"),
            "/api/v2/monitor/user/device/query": {
                "status": "success",
                "results": [{"mac": "AA:00:00:00:00:01", "ip": "192.168.1.50", "hostname": "nas"}],
            },
        }
    )

    result = _api(fake, retries=0).fetch_devices()

    assert result["available"] is True
    assert result["degraded"] is True
    assert result["items"][0]["hostname"] == "nas"
    assert result["missing"] == [
        {"item": "dhcp_leases", "reason": "FortiGate request failed after 1 attempt(s)"}
    ]


def test_policy_summary_explains_source_destination_and_action():
    fake = FakeHttpClient(
        {
            "/api/v2/cmdb/firewall/policy": {
                "status": "success",
                "results": [
                    {
                        "policyid": 41,
                        "srcintf": [{"name": "office"}, {"name": "tailscale0"}],
                        "dstintf": [{"name": "servers"}],
                        "action": "deny",
                    }
                ],
            }
        }
    )

    result = _api(fake).fetch_policies()

    assert result["purpose"].startswith("流量解释")
    assert result["items"] == [
        {
            "id": 41,
            "source_zones": ["office", "tailscale0"],
            "destination_zones": ["servers"],
            "action": "deny",
            "missing_fields": [],
        }
    ]


def test_config_revisions_form_a_change_ledger():
    fake = FakeHttpClient(
        {
            "/api/v2/monitor/system/config-revision": {
                "status": "success",
                "results": {
                    "version": "v7.0.15",
                    "revisions": [
                        {
                            "version": 318,
                            "created": 1787500000,
                            "user": "netadmin",
                            "comments": "policy 41 reviewed",
                        }
                    ]
                },
            }
        }
    )

    result = _api(fake).fetch_change_ledger()

    assert result["purpose"].startswith("变更账")
    assert result["items"] == [
        {
            "version": 318,
            "changed_at": 1787500000,
            "administrator": "netadmin",
            "summary": "policy 41 reviewed",
            "missing_fields": [],
        }
    ]


def test_unreachable_fortigate_retries_then_returns_explicit_missing_data():
    fake = FakeHttpClient(
        {
            "/api/v2/cmdb/system/interface": [TimeoutError("down")] * 3,
        }
    )

    result = _api(fake, retries=2).fetch_interfaces()

    assert len(fake.calls) == 3
    assert result["available"] is False
    assert result["degraded"] is True
    assert result["items"] is None
    assert result["missing"] == [
        {"item": "interfaces", "reason": "FortiGate request failed after 3 attempt(s)"}
    ]


def test_certificate_check_can_only_be_disabled_explicitly():
    fake = FakeHttpClient(
        {
            "/api/v2/cmdb/system/interface": {"status": "success", "results": []},
        }
    )

    _api(fake, verify_tls=False).fetch_interfaces()

    assert fake.calls[0]["verify_tls"] is False


def test_credentials_never_reach_repr_logs_results_or_exceptions(caplog):
    username = "user-private-91"
    password = "pass-private-37"
    fake = FakeHttpClient(
        {
            "/api/v2/cmdb/system/interface": [
                RuntimeError(f"failed with {username} and {password}")
            ],
        }
    )
    api = FortiGateReadonlyAPI(
        "https://192.168.1.1",
        username,
        password,
        http_client=fake,
        retries=0,
        clock=lambda: NOW,
    )

    result = api.fetch_interfaces()
    rendered = " ".join((repr(api), repr(result), json.dumps(result, ensure_ascii=False), caplog.text))
    assert username not in rendered
    assert password not in rendered

    with pytest.raises(ValueError) as error:
        FortiGateReadonlyAPI("https://embedded:credential@192.168.1.1", username, password)
    assert username not in str(error.value)
    assert password not in str(error.value)
    assert "credential@" not in str(error.value)


def test_module_exposes_no_write_named_method():
    blocked_names = ("post", "put", "delete", "set", "create", "update")
    offenders: list[str] = []
    for object_name, candidate in inspect.getmembers(fortigate_api):
        if inspect.isfunction(candidate) and candidate.__module__ == fortigate_api.__name__:
            if any(blocked in object_name.lower() for blocked in blocked_names):
                offenders.append(object_name)
        if inspect.isclass(candidate) and candidate.__module__ == fortigate_api.__name__:
            for method_name, method in inspect.getmembers(candidate, inspect.isfunction):
                if method_name.startswith("__"):
                    continue
                if any(blocked in method_name.lower() for blocked in blocked_names):
                    offenders.append(f"{object_name}.{method_name}")
    assert offenders == []


_LIVE_FORTIGATE = os.environ.get("AUTOPOIESIS_TEST_FORTIGATE") == "1"


@pytest.mark.live
@pytest.mark.skipif(
    not _LIVE_FORTIGATE,
    reason="AUTOPOIESIS_TEST_FORTIGATE=1 is required for the real FortiGate check",
)
def test_real_fortigate_read_is_explicitly_gated():
    result = FortiGateReadonlyAPI.from_env(timeout=5.0, retries=1).fetch_all()
    assert result["fetched_at"].endswith("Z")
    assert {"interfaces", "devices", "policies", "changes"}.issubset(result)
