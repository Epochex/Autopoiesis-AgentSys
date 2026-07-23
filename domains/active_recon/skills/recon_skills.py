from __future__ import annotations

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


SKILL_OPERATIONS = {
    "scan_ports": ("port_scan", ["port", "scan", "open", "exposed"]),
    "enum_services": ("service_enum", ["service", "enum", "http", "ssh", "database"]),
    "grab_banner": ("banner_grab", ["banner", "version", "fingerprint"]),
    "check_tls": ("tls_check", ["tls", "certificate", "cipher", "https"]),
    "match_cve": ("cve_match", ["cve", "vulnerability", "risk", "exposed"]),
    "probe_weak_credentials": ("weak_cred_check", ["credential", "password", "weak", "login"]),
    "probe_exploit": ("exploit_probe", ["exploit", "proof", "rce", "intrusive"]),
}


def register_active_recon_skills(registry: SkillRegistry, adapter) -> None:
    source_kind = "mock" if adapter.__class__.__name__.startswith("Mock") else "allowlisted live"
    for name, (operation, tags) in SKILL_OPERATIONS.items():
        readonly = operation in adapter.readonly_operations
        registry.register(
            SkillSpec(
                name=name,
                description=f"{source_kind} active recon probe for {operation}",
                input_schema={"case_id": "str"},
                risk="read_only" if readonly else "approval_required",
                cost=1.0,
                tags=tags,
            ),
            _handler(adapter, operation, name, readonly),
        )


def _handler(adapter, operation: str, skill_name: str, readonly: bool):
    def run(case) -> SkillResult:
        return SkillResult(
            skill_name=skill_name,
            evidence=adapter.query(case.id, operation),
            readonly=readonly,
            cost=1.0,
        )

    return run
