from __future__ import annotations

import shlex
import os
import gzip
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.env import autopoiesis_env

from pydantic import BaseModel, Field


class FortiOSLogEvent(BaseModel):
    timestamp: str | None = None
    type: str | None = None
    subtype: str | None = None
    level: str | None = None
    logid: str | None = None
    srcip: str | None = None
    dstip: str | None = None
    action: str | None = None
    policyid: str | None = None
    msg: str | None = None
    raw: str
    fields: dict[str, str] = Field(default_factory=dict)


def parse_fortios_kv_line(line: str) -> FortiOSLogEvent:
    fields: dict[str, str] = {}
    for token in shlex.split(line.strip()):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value.strip('"')

    timestamp = None
    if fields.get("date") and fields.get("time"):
        timestamp = f"{fields['date']}T{fields['time']}"

    return FortiOSLogEvent(
        timestamp=timestamp,
        type=fields.get("type"),
        subtype=fields.get("subtype"),
        level=fields.get("level"),
        logid=fields.get("logid"),
        srcip=fields.get("srcip"),
        dstip=fields.get("dstip"),
        action=fields.get("action"),
        policyid=fields.get("policyid"),
        msg=fields.get("msg"),
        raw=line.rstrip("\n"),
        fields=fields,
    )


class LocalFixtureLogAdapter:
    def __init__(self, path: str | Path | list[str | Path]):
        raw_paths = path if isinstance(path, list) else [path]
        self.paths = [Path(item) for item in raw_paths]

    def query(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[FortiOSLogEvent]:
        filters = filters or {}
        events = [
            parse_fortios_kv_line(line)
            for path in self.paths
            for line in _read_lines(path)
            if line.strip()
        ]
        return [event for event in events if _matches(event, filters) and _within_time_window(event, start, end)]


class R230IngestorLogAdapter:
    """Readonly HTTP client for the R230 ingestor; disabled unless constructed by caller."""

    def __init__(self, base_url: str | None = None, bearer_token: str | None = None):
        if autopoiesis_env("ENABLE_R230_INGESTOR") != "1":
            raise RuntimeError("R230IngestorLogAdapter is disabled by default")
        resolved_base_url = base_url or os.environ["R230_INGESTOR_URL"]
        self.base_url = resolved_base_url.rstrip("/")
        self.bearer_token = bearer_token or os.getenv("R230_INGESTOR_TOKEN")

    def query(self, *, start: str, end: str, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        params = {"start": start, "end": end, **(filters or {})}
        request = Request(f"{self.base_url}/logs?{urlencode(params)}", method="GET")
        if self.bearer_token:
            request.add_header("Authorization", f"Bearer {self.bearer_token}")
        with urlopen(request, timeout=10) as response:
            import json

            return json.loads(response.read().decode("utf-8"))


def _matches(event: FortiOSLogEvent, filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if event.fields.get(key) != expected:
            return False
    return True


def _read_lines(path: Path) -> list[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read().splitlines()
    return path.read_text(encoding="utf-8").splitlines()


def _within_time_window(event: FortiOSLogEvent, start: datetime | None, end: datetime | None) -> bool:
    if not start and not end:
        return True
    if event.timestamp is None:
        return False
    try:
        timestamp = datetime.fromisoformat(event.timestamp)
    except ValueError:
        return False
    if start and timestamp < start:
        return False
    if end and timestamp > end:
        return False
    return True
