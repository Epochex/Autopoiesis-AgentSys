from __future__ import annotations

import socket
from pathlib import Path

from pydantic import BaseModel, Field

from domains.network_rca.real_dataset import load_manifest, validate_real_dataset_manifest


DEFAULT_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "real" / "manifest.json"


class RealDataReadiness(BaseModel):
    r230_host: str = "192.168.1.23"
    syslog_port_open: bool = False
    ingestor_port_open: bool = False
    manifest_path: str = ""
    manifest_exists: bool = False
    manifest_valid: bool = False
    real_syslog_files: list[str] = Field(default_factory=list)
    manifest_errors: list[str] = Field(default_factory=list)
    blocked: bool = True
    reason: str = ""


def probe_r230_readiness(
    host: str = "192.168.1.23",
    manifest_path: str | Path | None = None,
) -> RealDataReadiness:
    manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST
    syslog_open = _is_open(host, 514)
    ingestor_open = any(_is_open(host, port) for port in (8000, 8026, 8080, 9090))

    manifest_exists = manifest.exists()
    validation = validate_real_dataset_manifest(manifest) if manifest_exists else None
    manifest_valid = bool(validation and validation.ready)
    errors = list(validation.errors) if validation else ["manifest.json does not exist"]

    real_files: list[str] = []
    if manifest_valid:
        loaded = load_manifest(manifest)
        base = manifest.parent
        real_files = [
            str(p if (p := Path(item)).is_absolute() else base / item) for item in loaded.syslog_paths
        ]

    # Readiness is gated on a real, validatable held-out dataset being present
    # locally. The R230 ingestor is optional; a local export is sufficient.
    blocked = not manifest_valid
    if blocked:
        reason = (
            "No validated real held-out dataset is present. "
            + ("manifest.json is missing; " if not manifest_exists else "manifest invalid; ")
            + ("; ".join(errors[:3]) if errors else "")
        ).strip()
    else:
        reason = "Validated real FortiGate held-out dataset is present; real held-out eval can run."

    return RealDataReadiness(
        r230_host=host,
        syslog_port_open=syslog_open,
        ingestor_port_open=ingestor_open,
        manifest_path=str(manifest),
        manifest_exists=manifest_exists,
        manifest_valid=manifest_valid,
        real_syslog_files=real_files,
        manifest_errors=errors if not manifest_valid else [],
        blocked=blocked,
        reason=reason,
    )


def _is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
