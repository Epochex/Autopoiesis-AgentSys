from __future__ import annotations

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


# Readonly skills for real FortiGate syslog RCA. Each maps to a RealSyslogAdapter operation.
REAL_SKILL_OPERATIONS = {
    "check_admin_auth_failures": ("admin_auth_failures", ["admin", "login", "auth", "failed", "bruteforce"]),
    "check_admin_lockout": ("admin_lockout", ["admin", "lockout", "disabled", "login"]),
    "check_policy_deny_profile": ("policy_deny_profile", ["deny", "policy", "port", "netbios", "traffic"]),
    "check_traffic_baseline": ("traffic_baseline", ["traffic", "accept", "baseline", "forwarding"]),
    "check_event_log": ("event_log_scan", ["event", "session", "clash", "update"]),
}


def register_real_rca_skills(registry: SkillRegistry, adapter) -> None:
    for name, (operation, tags) in REAL_SKILL_OPERATIONS.items():
        registry.register(
            SkillSpec(
                name=name,
                description=f"Readonly real-syslog RCA check for {operation}",
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
