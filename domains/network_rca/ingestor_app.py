from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from domains.network_rca.adapters.fortios_syslog import LocalFixtureLogAdapter


DEFAULT_R230_FORTIGATE_LOG_PATH = "/data/fortigate-runtime/input/fortigate.log"


def create_app(log_paths: list[str | Path] | None = None):
    try:
        from fastapi import FastAPI, Query
    except ImportError as exc:
        raise RuntimeError("Install the ingestor extra to run this app: pip install -e '.[ingestor]'") from exc

    resolved_paths = [Path(item) for item in (log_paths or _paths_from_env())]
    adapter = LocalFixtureLogAdapter(resolved_paths)
    app = FastAPI(title="Autopoiesis R230 readonly FortiGate log ingestor")

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "readonly": True,
            "log_files": [str(path) for path in resolved_paths],
            "existing_log_files": [str(path) for path in resolved_paths if path.exists()],
        }

    @app.get("/logs")
    def logs(
        start: datetime | None = None,
        end: datetime | None = None,
        type: str | None = Query(default=None),
        subtype: str | None = Query(default=None),
        level: str | None = Query(default=None),
        action: str | None = Query(default=None),
        policyid: str | None = Query(default=None),
    ) -> list[dict]:
        filters = {
            key: value
            for key, value in {
                "type": type,
                "subtype": subtype,
                "level": level,
                "action": action,
                "policyid": policyid,
            }.items()
            if value is not None
        }
        return [
            event.model_dump(mode="json")
            for event in adapter.query(start=start, end=end, filters=filters)
        ]

    return app


def _paths_from_env() -> list[str]:
    raw = os.getenv("R230_FORTIGATE_LOG_PATHS", DEFAULT_R230_FORTIGATE_LOG_PATH)
    return [item.strip() for item in raw.split(":") if item.strip()]


try:
    app = create_app()
except RuntimeError:
    app = None
