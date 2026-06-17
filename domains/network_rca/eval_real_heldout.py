from __future__ import annotations

import argparse
import json

from domains.network_rca.eval import compare_baselines
from domains.network_rca.real_dataset import load_real_case_bundle, validate_real_dataset_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run baseline comparison on a real held-out RCA manifest.")
    parser.add_argument("manifest", help="Path to a real dataset manifest.json")
    parser.add_argument("--reasoner-mode", default="rule", choices=["rule", "llm"])
    args = parser.parse_args()

    validation = validate_real_dataset_manifest(args.manifest)
    if not validation.ready:
        print(json.dumps({"validation": validation.model_dump()}, indent=2, sort_keys=True))
        raise SystemExit(2)

    cases, ground_truth = load_real_case_bundle(args.manifest, split="heldout")
    rows = compare_baselines(cases, ground_truth, reasoner_mode=args.reasoner_mode)
    print(
        json.dumps(
            {
                "validation": validation.model_dump(),
                "heldout_baselines": [row.model_dump() for row in rows],
                "warning": "Only dataset_kind=real and split=heldout rows should be used as real RCA metrics.",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
