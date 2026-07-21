"""Build the console snapshot from the REAL network_rca framework.

Everything served here comes from the actual Autopoiesis pipeline running on the
real R230 FortiGate held-out dataset: readiness, data stats, the held-out
baseline/ablation table, and per-case diagnosis + cited evidence + trace.
No hardcoded incident fixtures.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Make the repo root importable so the gateway can drive the real framework.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from domains.network_rca.eval import compare_baselines  # noqa: E402
from domains.network_rca.factory import build_network_rca_orchestrator  # noqa: E402
from domains.network_rca.real_data_readiness import probe_r230_readiness  # noqa: E402
from domains.network_rca.real_dataset import (  # noqa: E402
    load_real_case_bundle,
    resolve_stats_path,
    validate_real_dataset_manifest,
)

_MANIFEST = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "manifest.json"
_TOPOLOGY = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_topology.json"


_MESH = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_mesh.json"
_DEVICE_GRAPH = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_device_graph.json"


def _load_device_graphs() -> dict[str, Any]:
    """Full per-subnet device graphs mined from the raw syslog (see build_device_graph)."""
    try:
        return json.loads(_DEVICE_GRAPH.read_text(encoding="utf-8")).get("graphs", {})
    except Exception:
        return {}


def _load_topology() -> dict[str, Any] | None:
    try:
        topo = json.loads(_TOPOLOGY.read_text(encoding="utf-8"))
    except Exception:
        return None
    # The device graph is the single source of truth for how many hosts a segment
    # actually has — the fixture's `hosts` was a stale count from an older capture,
    # so the map claimed 103 devices while only the flagged handful could open.
    graphs = _load_device_graphs()
    for sub in topo.get("subnets", []):
        g = graphs.get(sub.get("cidr"))
        if g:
            sub["hosts"] = g["stats"]["devices"]
            sub["graphEdges"] = g["stats"]["edges"]
    return topo


def _load_meshes() -> dict[str, Any]:
    try:
        return json.loads(_MESH.read_text(encoding="utf-8")).get("meshes", {})
    except Exception:
        return {}


def _data_stats(stats_path: Path) -> dict[str, Any]:
    s = json.loads(stats_path.read_text(encoding="utf-8"))
    return {
        "source": "DAHUA_FORTIGATE (FG100E) via R230 192.168.1.23",
        "windowDays": s.get("window_days", []),
        "adminLoginFailed": s.get("admin_login_failed", 0),
        "distinctSrc": s.get("admin_login_failed_distinct_src", 0),
        "topAttackerSrc": s.get("admin_login_failed_top_src", [])[:5],
        "lockouts": s.get("admin_login_disabled_lockouts", 0),
        "denyCount": s.get("deny_count", 0),
        "topDenyPorts": s.get("deny_top_dstports", [])[:5],
        "topDenySrc": s.get("deny_top_src", [])[:5],
        "acceptPermit": s.get("accept_permit_count", 0),
        "sessionClash": s.get("session_clash", 0),
    }


def _run_case(case, stats_path: Path, reasoner_mode: str) -> dict[str, Any]:
    with TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "trace.jsonl"
        orch = build_network_rca_orchestrator(
            ledger, data_source="real", real_stats_path=stats_path, reasoner_mode=reasoner_mode
        )
        diagnosis, report = orch.diagnose(case)
        trace = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "id": case.id,
        "title": case.title,
        "query": case.query,
        "assets": case.assets,
        "diagnosis": {
            "rootCauseKey": diagnosis.root_cause_key,
            "rootCause": diagnosis.root_cause,
            "confidence": round(diagnosis.confidence, 3),
            "readonly": diagnosis.readonly,
            "evidence": [
                {"evidenceId": e.evidence_id, "source": e.source, "summary": e.summary}
                for e in diagnosis.evidence
            ],
            "recommendedActions": diagnosis.recommended_actions,
        },
        "verifier": {"passed": report.passed, "errors": list(report.errors)},
        "trace": [{"kind": ev.get("kind"), "payload": ev.get("payload", {})} for ev in trace],
    }


def load_rca_snapshot(manifest_path: Path | None = None, provider_id: str = "rule") -> dict[str, Any]:
    from . import providers

    manifest = Path(manifest_path) if manifest_path else _MANIFEST
    reasoner_mode, llm_env = providers.resolve_reasoner(provider_id)
    if llm_env:
        os.environ.update(llm_env)
    readiness = probe_r230_readiness(manifest_path=manifest)
    validation = validate_real_dataset_manifest(manifest)

    payload: dict[str, Any] = {
        "readiness": {
            "blocked": readiness.blocked,
            "reason": readiness.reason,
            "syslogPortOpen": readiness.syslog_port_open,
            "manifestValid": readiness.manifest_valid,
        },
        "datasetReady": validation.ready,
        "provider": provider_id,
        "reasonerMode": reasoner_mode,
        "providers": providers.list_providers(),
        "providerError": None,
        "topology": _load_topology(),
        "meshes": _load_meshes(),
        "cases": [],
        "baselines": [],
        "dataStats": None,
        "note": (
            f"Live data from the real network_rca framework on the R230 FortiGate held-out set "
            f"(reasoner: {reasoner_mode})."
        ),
    }

    if not validation.ready:
        payload["note"] = "No validated real held-out dataset present. " + readiness.reason
        return payload

    stats_path = resolve_stats_path(manifest)
    payload["dataStats"] = _data_stats(stats_path)

    cases, ground_truth = load_real_case_bundle(manifest, split="heldout")
    try:
        if reasoner_mode == "llm":
            # Parallelize the per-case LLM diagnoses so the live request stays responsive.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(6, len(cases) or 1)) as pool:
                payload["cases"] = list(pool.map(lambda c: _run_case(c, stats_path, "llm"), cases))
        else:
            payload["cases"] = [_run_case(case, stats_path, "rule") for case in cases]
        # The ablation is a skill-control property (engine-independent); run it with the
        # instant rule reasoner so the live request never blocks on the LLM endpoint.
        payload["baselines"] = [
            {
                "name": row.name,
                "rootCauseAccuracy": row.root_cause_accuracy,
                "evidenceRecall": row.evidence_recall,
                "verifierPassRate": row.verifier_pass_rate,
                "cases": row.cases,
                "notes": row.notes,
            }
            for row in compare_baselines(
                cases, ground_truth, data_source="real", real_stats_path=stats_path,
                reasoner_mode="rule",
            )
        ]
    except Exception as exc:  # LLM endpoint unreachable / misconfigured — stay honest.
        payload["providerError"] = f"{type(exc).__name__}: {exc}"
        payload["note"] = (
            f"Provider '{provider_id}' failed ({type(exc).__name__}). The endpoint is likely "
            f"down or missing a key. Switch back to the rule baseline or open the GPU tunnel."
        )
    return payload


def load_evolution(manifest_path: Path | None = None, passes: int = 4) -> dict[str, Any]:
    """Real self-evolution on the held-out stream (cold-vs-warm, StreamBench-style).

    Recurring incidents may use provenance-linked memory to narrow the probe plan,
    but every diagnosis still obtains fresh evidence. Historical snapshots are
    never replayed as current observations.
    """
    from core.evolve import compare_cold_vs_warm

    manifest = Path(manifest_path) if manifest_path else _MANIFEST
    validation = validate_real_dataset_manifest(manifest)
    if not validation.ready:
        return {"ready": False, "reason": "No validated real held-out dataset present."}
    stats_path = resolve_stats_path(manifest)
    cases, ground_truth = load_real_case_bundle(manifest, split="heldout")
    res = compare_cold_vs_warm(
        cases, ground_truth, passes=passes,
        data_source="real", real_stats_path=stats_path, reasoner_mode="rule",
    )
    # The warm run is the only one with a memory lifecycle (cold has evolve=False).
    # Move it to the top level rather than copying, so the payload isn't doubled.
    observatory = res["warm"].pop("observatory", None)
    return {
        "ready": True,
        "passes": passes,
        "nCases": len(cases),
        "cases": [
            {
                "id": c.id,
                "query": c.query,
                "assets": list(c.assets),
                "rootCauseKey": ground_truth[c.id].expected_root_cause_key if c.id in ground_truth else "",
            }
            for c in cases
        ],
        "warm": res["warm"],
        "cold": res["cold"],
        "delta": res["delta"],
        "memory": res.get("memory", {}),
        "observatory": observatory,
    }
