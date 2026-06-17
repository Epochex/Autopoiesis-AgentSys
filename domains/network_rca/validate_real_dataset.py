from __future__ import annotations

import json
import os
from pathlib import Path

from domains.network_rca.real_dataset import validate_real_dataset_manifest


DEFAULT_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "real" / "manifest.json"


def main() -> None:
    manifest = Path(os.getenv("SELFEVO_REAL_DATASET_MANIFEST", str(DEFAULT_MANIFEST)))
    report = validate_real_dataset_manifest(manifest)
    print(json.dumps(report.model_dump(), indent=2, sort_keys=True))
    raise SystemExit(0 if report.ready else 2)


if __name__ == "__main__":
    main()
