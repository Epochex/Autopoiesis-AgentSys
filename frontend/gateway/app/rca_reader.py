"""Build the console snapshot from the REAL network_rca framework.

Everything served here comes from the actual selfevo pipeline running on the
real R230 FortiGate held-out dataset: readiness, data stats, the held-out
baseline/ablation table, and per-case diagnosis + cited evidence + trace.
No hardcoded incident fixtures.
"""
from __future__ import annotations

import json
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


def _run_case(case, stats_path: Path) -> dict[str, Any]:
    with TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "trace.jsonl"
        orch = build_network_rca_orchestrator(
            ledger, data_source="real", real_stats_path=stats_path
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


def load_rca_snapshot(manifest_path: Path | None = None) -> dict[str, Any]:
    manifest = Path(manifest_path) if manifest_path else _MANIFEST
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
        "cases": [],
        "baselines": [],
        "dataStats": None,
        "note": (
            "Live data from the real network_rca framework on the R230 FortiGate held-out set. "
            "Baseline rows use the deterministic rule reasoner; full_tools removes skill control."
        ),
    }

    if not validation.ready:
        payload["note"] = "No validated real held-out dataset present. " + readiness.reason
        return payload

    stats_path = resolve_stats_path(manifest)
    payload["dataStats"] = _data_stats(stats_path)

    cases, ground_truth = load_real_case_bundle(manifest, split="heldout")
    payload["cases"] = [_run_case(case, stats_path) for case in cases]
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
            cases, ground_truth, data_source="real", real_stats_path=stats_path
        )
    ]
    return payload
