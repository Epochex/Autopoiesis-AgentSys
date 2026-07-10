from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


class MockEnterpriseSystem:
    """Deterministic fixture-backed enterprise system; no external I/O."""

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self._initial = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        self._state = deepcopy(self._initial)
        self.drop_next_write = False

    @classmethod
    def from_path(cls, path: str | Path) -> "MockEnterpriseSystem":
        return cls(path)

    def reset(self) -> None:
        self._state = deepcopy(self._initial)
        self.drop_next_write = False

    def snapshot(self, case_id: str) -> dict[str, Any]:
        return deepcopy(self._state[case_id])

    def has_case(self, case_id: str) -> bool:
        return case_id in self._state

    def restore(self, case_id: str, state: dict[str, Any]) -> None:
        """Compensating rollback: reinstate a snapshot after a rejected step."""
        self._state[case_id] = deepcopy(state)

    def apply_pricing(self, case_id: str) -> dict[str, Any]:
        state = self._state[case_id]
        policy = state["policy"]
        quoted = round(float(state["base_price"]) * float(policy["multiplier"]) - float(policy.get("discount", 0)), 2)
        effect = {"quote_price": quoted, "pricing_status": "quoted"}
        if not self.drop_next_write:
            state.update(effect)
        self.drop_next_write = False
        return effect

    def submit_approval(self, case_id: str) -> dict[str, Any]:
        state = self._state[case_id]
        effect = {"approval_submitted": True, "status": "pending_approval"}
        if not self.drop_next_write:
            state.update(effect)
        self.drop_next_write = False
        return effect

    def mark_approved(self, case_id: str) -> dict[str, Any]:
        state = self._state[case_id]
        effect = {"status": "approved"}
        if not self.drop_next_write:
            state.update(effect)
        self.drop_next_write = False
        return effect

    def send_reminder(self, case_id: str) -> dict[str, Any]:
        state = self._state[case_id]
        effect = {"reminder_sent": True}
        if not self.drop_next_write:
            state.update(effect)
        self.drop_next_write = False
        return effect
