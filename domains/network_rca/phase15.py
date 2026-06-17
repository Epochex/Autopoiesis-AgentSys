from __future__ import annotations

import json

from domains.network_rca.eval import compare_baselines
from domains.network_rca.factory import load_ground_truth, load_seed_cases
from domains.network_rca.real_data_readiness import probe_r230_readiness


def main() -> None:
    cases = load_seed_cases()
    ground_truth = load_ground_truth()
    payload = {
        "real_data_readiness": probe_r230_readiness().model_dump(),
        "mock_baselines": [row.model_dump() for row in compare_baselines(cases, ground_truth)],
        "warning": "mock_baselines are pipeline checks only; they are not real held-out RCA quality metrics.",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
