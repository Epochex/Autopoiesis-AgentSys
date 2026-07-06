#!/usr/bin/env python3
"""Reproduce every headline number in docs/BENCHMARKS.md in one run.

    python3 examples/benchmarks.py

Prefers the REAL R230 FortiGate held-out set (via the dataset manifest); if that is
not present it falls back to the in-repo seed cases and says so loudly. The ablation
collapse (skill-scheduling → 16.7%) is a property of the REAL held-out set — the mock
seed cases do NOT exhibit it, and the output labels which set produced the table.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root importable

from core.evolve import compare_cold_vs_warm, run_evolving_stream  # noqa: E402
from domains.network_rca.eval import compare_baselines  # noqa: E402
from domains.network_rca.factory import load_ground_truth, load_seed_cases  # noqa: E402

_MANIFEST = Path(__file__).resolve().parents[1] / "domains" / "network_rca" / "fixtures" / "real" / "manifest.json"


def _real_bundle():
    """Return (cases, ground_truth, kwargs, label) for the real held-out set, or None."""
    try:
        from domains.network_rca.real_dataset import (
            load_real_case_bundle,
            resolve_stats_path,
            validate_real_dataset_manifest,
        )
        if not validate_real_dataset_manifest(_MANIFEST).ready:
            return None
        stats_path = resolve_stats_path(_MANIFEST)
        cases, gt = load_real_case_bundle(_MANIFEST, split="heldout")
        return cases, gt, {"data_source": "real", "real_stats_path": stats_path}, "REAL R230 FortiGate held-out"
    except Exception:
        return None


def main() -> int:
    real = _real_bundle()
    if real is not None:
        cases, gt, kwargs, label = real
    else:
        cases, gt, kwargs, label = load_seed_cases(), load_ground_truth(), {}, "SEED cases (mock — real dataset absent)"

    print(f"# dataset: {label}  ({len(cases)} cases, rule reasoner)\n")

    print("## 1. Self-evolution (cold vs warm, passes=4)")
    res = compare_cold_vs_warm(cases, gt, passes=4, **kwargs)
    d = res["delta"]
    print(f"   probes   : {d['probes_cold']} -> {d['probes_warm']}  (-{d['probes_saved_pct']}%)")
    print(f"   cost     : {d['cost_cold']} -> {d['cost_warm']}  (-{d['cost_saved_pct']}%)")
    print(f"   accuracy : warm {d['accuracy_warm']} / cold {d['accuracy_cold']}  (must be equal)")
    print(f"   memory   : 0 -> {d['memory_grown']}\n")

    print("## 2. Ablation (per-component, root-cause accuracy)")
    for r in compare_baselines(cases, gt, reasoner_mode="rule", **kwargs):
        tag = "  <-- load-bearing" if r.root_cause_accuracy < 0.5 else ""
        print(f"   {r.name:24s} acc={r.root_cause_accuracy:.3f}{tag}")
    if not kwargs:
        print("   (note: the skill-scheduling collapse only appears on the REAL held-out set)")
    print()

    print("## 3. Memory health (Phase B — managed store)")
    warm = run_evolving_stream(cases, gt, passes=4, evolve=True, **kwargs)
    h = warm.get("memory_health", {})
    print(f"   active={h.get('active')}  links={h.get('links')}  insights={h.get('insights')}  "
          f"forgotten={h.get('forgotten')}  by_tier={h.get('by_tier')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
