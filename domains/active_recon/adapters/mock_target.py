from __future__ import annotations

import json
from pathlib import Path


class MockTargetAdapter:
    readonly_operations = {
        "port_scan",
        "service_enum",
        "banner_grab",
        "tls_check",
        "cve_match",
    }
    approval_required_operations = {"weak_cred_check", "exploit_probe"}
    operations = readonly_operations | approval_required_operations

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self.responses = json.loads(self.fixture_path.read_text(encoding="utf-8"))

    @classmethod
    def from_path(cls, path: str | Path) -> "MockTargetAdapter":
        return cls(path)

    def query(self, case_id: str, operation: str) -> list[dict]:
        if operation not in self.operations:
            raise ValueError(f"unknown mock target operation: {operation}")
        case_responses = self.responses.get(case_id, {})
        return case_responses.get(operation, [])
