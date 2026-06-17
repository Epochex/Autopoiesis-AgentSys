from __future__ import annotations

import socket
from pathlib import Path

from pydantic import BaseModel, Field


class RealDataReadiness(BaseModel):
    r230_host: str = "192.168.1.23"
    syslog_port_open: bool = False
    http_port_open: bool = False
    ingestor_port_open: bool = False
    local_real_syslog_files: list[str] = Field(default_factory=list)
    blocked: bool = True
    reason: str


def probe_r230_readiness(host: str = "192.168.1.23") -> RealDataReadiness:
    syslog_open = _is_open(host, 514)
    http_open = _is_open(host, 80)
    ingestor_open = any(_is_open(host, port) for port in (8000, 8026, 8080, 9090))
    local_files = [
        str(path)
        for path in Path("/data").glob("**/*fortigate*.log")
        if "selfevo-orchiter/domains/network_rca/fixtures" not in str(path)
    ][:20]
    blocked = not local_files or not ingestor_open
    if blocked:
        reason = (
            "R230 syslog port is reachable, but no readonly ingestor API was detected on 8000/8026/8080/9090 "
            "and no local 3-7 day FortiGate syslog export exists under /data."
        )
    else:
        reason = "readonly ingestor and local real syslog fixtures detected"
    return RealDataReadiness(
        r230_host=host,
        syslog_port_open=syslog_open,
        http_port_open=http_open,
        ingestor_port_open=ingestor_open,
        local_real_syslog_files=local_files,
        blocked=blocked,
        reason=reason,
    )


def _is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
