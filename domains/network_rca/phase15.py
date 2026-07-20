from __future__ import annotations

import json
from pathlib import Path

from core.env import autopoiesis_env
from domains.network_rca.eval import compare_baselines
from domains.network_rca.factory import load_ground_truth, load_seed_cases
from domains.network_rca.real_data_readiness import probe_r230_readiness
from domains.network_rca.real_dataset import (
    load_real_case_bundle,
    resolve_stats_path,
    validate_real_dataset_manifest,
)


def main() -> None:
    cases = load_seed_cases()
    ground_truth = load_ground_truth()
    manifest_path = Path(
        autopoiesis_env(
            "REAL_DATASET_MANIFEST",
            str(Path(__file__).resolve().parent / "fixtures" / "real" / "manifest.json"),
        )
    )
    manifest_validation = validate_real_dataset_manifest(manifest_path)
    payload = {
        "real_data_readiness": probe_r230_readiness(manifest_path=manifest_path).model_dump(),
        "real_dataset_manifest": manifest_validation.model_dump(),
        "mock_baselines": [row.model_dump() for row in compare_baselines(cases, ground_truth)],
        "warning": "mock_baselines are pipeline checks only; they are not real held-out RCA quality metrics.",
    }
    if manifest_validation.ready:
        real_cases, real_truth = load_real_case_bundle(manifest_path, split="heldout")
        payload["real_heldout_baselines"] = [
            row.model_dump()
            for row in compare_baselines(
                real_cases,
                real_truth,
                data_source="real",
                real_stats_path=resolve_stats_path(manifest_path),
            )
        ]
        payload["real_heldout_note"] = (
            "rule reasoner is a deterministic baseline on real held-out FortiGate data; "
            "it is not a proof of RCA reasoning quality. LLM-reasoner held-out eval requires a configured endpoint."
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
