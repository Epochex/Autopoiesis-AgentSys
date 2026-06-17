from __future__ import annotations

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


SKILL_OPERATIONS = {
    "check_interface_status": ("interface_status", ["interface", "carrier", "eno1", "eno2"]),
    "check_link_carrier": ("link_carrier", ["carrier", "link", "peer"]),
    "check_lacp": ("lacp", ["lacp", "eth-trunk", "office"]),
    "route_between_segments": ("route", ["route", "segment", "fortigate"]),
    "check_dhcp": ("dhcp", ["dhcp", "lease"]),
    "check_fw_policy": ("fw_policy", ["policy", "fortigate", "address"]),
    "check_wan_health": ("wan_health", ["wan", "internet", "forwarding"]),
    "check_switch_vlan": ("switch_vlan", ["vlan", "switch", "huawei"]),
    "check_vip_mapping": ("vip", ["vip", "nat", "port"]),
    "check_security_subscription": ("security_subscription", ["fortiguard", "av", "ips", "webfilter"]),
}


def register_network_rca_skills(registry: SkillRegistry, adapter) -> None:
    for name, (operation, tags) in SKILL_OPERATIONS.items():
        registry.register(
            SkillSpec(
                name=name,
                description=f"Readonly RCA check for {operation}",
                input_schema={"case_id": "str"},
                risk="read_only",
                cost=1.0,
                tags=tags,
            ),
            _handler(adapter, operation, name),
        )


def _handler(adapter, operation: str, skill_name: str):
    def run(case) -> SkillResult:
        return SkillResult(
            skill_name=skill_name,
            evidence=adapter.query(case.id, operation),
            readonly=True,
            cost=1.0,
        )

    return run
