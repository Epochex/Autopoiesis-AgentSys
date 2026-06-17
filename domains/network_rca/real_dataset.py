from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class RealDatasetManifest(BaseModel):
    dataset_id: str
    dataset_kind: Literal["real"]
    source_host: str = "192.168.1.23"
    captured_days: int
    syslog_paths: list[str] = Field(default_factory=list)
    train_cases_path: str
    heldout_cases_path: str
    notes: str = ""


class RealDatasetValidation(BaseModel):
    manifest_path: str
    ready: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def load_manifest(path: str | Path) -> RealDatasetManifest:
    return RealDatasetManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def validate_real_dataset_manifest(path: str | Path) -> RealDatasetValidation:
    manifest_path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not manifest_path.exists():
        return RealDatasetValidation(
            manifest_path=str(manifest_path),
            ready=False,
            errors=["manifest file does not exist"],
        )

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        return RealDatasetValidation(manifest_path=str(manifest_path), ready=False, errors=[str(exc)])

    base = manifest_path.parent
    if manifest.captured_days < 3:
        errors.append("captured_days must be at least 3")
    if not manifest.syslog_paths:
        errors.append("syslog_paths must list exported FortiGate syslog files")
    for item in manifest.syslog_paths:
        if not _resolve(base, item).exists():
            errors.append(f"syslog file missing: {item}")
    for case_path_name in ("train_cases_path", "heldout_cases_path"):
        case_path = _resolve(base, getattr(manifest, case_path_name))
        if not case_path.exists():
            errors.append(f"{case_path_name} missing: {case_path}")
            continue
        try:
            raw = json.loads(case_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{case_path_name} is not valid JSON: {exc}")
            continue
        if not isinstance(raw, list) or not raw:
            errors.append(f"{case_path_name} must be a non-empty list")
            continue
        for index, item in enumerate(raw):
            truth = item.get("ground_truth", {}) if isinstance(item, dict) else {}
            if truth.get("dataset_kind") != "real":
                errors.append(f"{case_path_name}[{index}] ground_truth.dataset_kind must be real")

    if manifest.captured_days < 7:
        warnings.append("captured_days is below the preferred 7-day upper target")

    return RealDatasetValidation(manifest_path=str(manifest_path), ready=not errors, errors=errors, warnings=warnings)


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
