from __future__ import annotations

import json
from pathlib import Path

from core.eval.replay import run_and_evaluate_replay
from domains.network_rca.factory import build_network_rca_orchestrator, load_ground_truth, load_seed_cases


def main() -> None:
    out = Path("artifacts/network_rca_phase1_trace.jsonl")
    if out.exists():
        out.unlink()
    orchestrator = build_network_rca_orchestrator(out)
    metrics = run_and_evaluate_replay(orchestrator, load_seed_cases(), load_ground_truth())
    print(json.dumps(metrics.model_dump(), indent=2, sort_keys=True))
    print(str(out))


if __name__ == "__main__":
    main()
