from __future__ import annotations

import json
from pathlib import Path


class MockDeviceAdapter:
    readonly_operations = {
        "interface_status",
        "link_carrier",
        "lacp",
        "route",
        "dhcp",
        "fw_policy",
        "wan_health",
        "switch_vlan",
        "vip",
        "security_subscription",
    }

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self.responses = json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def query(self, case_id: str, operation: str) -> list[dict]:
        if operation not in self.readonly_operations:
            raise PermissionError(f"operation is not readonly: {operation}")
        case_responses = self.responses.get(case_id, {})
        return case_responses.get(operation, [])
