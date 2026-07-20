from __future__ import annotations

import os

from core.env import autopoiesis_env


class LiveDeviceAdapter:
    """Feature-flagged placeholder. It never runs unless explicitly enabled."""

    def __init__(self):
        if autopoiesis_env("ENABLE_LIVE_DEVICE_ADAPTER") != "1":
            raise RuntimeError("LiveDeviceAdapter is disabled by default")
        self.host = os.environ["FORTIGATE_HOST"]
        self.user = os.environ["FORTIGATE_USER"]
        self.password = os.environ["FORTIGATE_PASS"]

    def query(self, case_id: str, operation: str) -> list[dict]:
        raise NotImplementedError("live readonly API integration belongs behind the feature flag")
