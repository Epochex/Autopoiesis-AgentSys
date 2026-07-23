"""Read the NetOps real-time subsystem's landed output for the live-situation panel.

The NetOps stream processor (a separate set of `python -m core.*` consumers on the
k3s `netops-core` namespace) de-Kafkas its two human-meaningful topics onto plain
disk sinks:

    netops.alerts.v1            → {runtime}/alerts/alerts-<YYYYMMDD-HH>.jsonl
    netops.aiops.suggestions.v1 → {runtime}/aiops/suggestions-<YYYYMMDD-HH>.jsonl
    (rolling window state)      → {runtime}/aiops/cluster-state.json

This module TAILS those files. It never opens a Kafka/Redpanda client and never
shares process state with NetOps — the Autopoiesis gateway and the NetOps pipeline
stay two decoupled subsystems that meet only at the read-only disk sink. That
boundary is deliberate (see RESUME_CLAIM_BANK): the two are one project, never one
process.

Everything surfaced here is a faithful projection of a real landed record. The
suggestion mapping renames fields to the frontend contract but invents no values;
the one derived field, `reviewVerdict.checks.overreachRisk.status`, is computed from
the real `approval_required` flag and stated as such.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Settings

# Suggestion sink files can reach 200+ MB/hour under stress load, so never json.load
# the whole file — read a bounded window off the tail and keep the last N lines.
_TAIL_BYTES = 512 * 1024
_ALERT_FEED = 12
_SUGGESTION_FEED = 8

# The fixed left-to-right pipeline the NetOps subsystem runs, named once so the
# snapshot, the stream delta and the frontend all speak the same stage vocabulary.
_ALERT_PATH = ["correlator", "alerts-topic", "cluster-window"]
_SUGGESTION_PATH = ["cluster-window", "aiops-agent", "suggestions-topic", "remediation"]


def _latest_file(directory: Path, prefix: str) -> Path | None:
    """Newest-by-mtime `prefix-*.jsonl` in `directory`, or None if the dir is absent."""
    try:
        candidates = [
            p for p in directory.iterdir()
            if p.name.startswith(prefix) and p.name.endswith(".jsonl")
        ]
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _tail_records(path: Path, count: int) -> list[dict[str, Any]]:
    """Last `count` JSON objects from a JSONL file, read within a bounded tail window.

    Reads at most `_TAIL_BYTES` off the end so a multi-hundred-MB sink never loads
    whole. The first (possibly truncated) line in that window is dropped.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
                blob = fh.read()
                blob = blob.split(b"\n", 1)[1] if b"\n" in blob else blob
            else:
                blob = fh.read()
    except (FileNotFoundError, OSError):
        return []
    out: list[dict[str, Any]] = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn tail line, skip it
    return out[-count:]


def _stage(stage_id: str, label: str, *, provider: str = "", ts: str = "", detail: str = "") -> dict[str, Any]:
    return {"stageId": stage_id, "label": label, "provider": provider, "ts": ts, "detail": detail}


def _stage_telemetry(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-stage telemetry reconstructed from the suggestion's own provenance.

    `correlator` and `aiops-agent` are always present (the contract asserts it); the
    critique/runbook stages appear only when the record carries their stage request.
    """
    ctx = raw.get("context") or {}
    adaptive = raw.get("adaptive_analysis") or {}
    inference = raw.get("inference") or {}
    reqs = raw.get("reasoning_stage_requests") or {}
    cluster_size = ctx.get("cluster_size", 0)
    stages = [
        _stage(
            "correlator", "关联窗口",
            ts=ctx.get("cluster_first_alert_ts", ""),
            detail=f"簇 {cluster_size} · 近1h复发 {ctx.get('recent_similar_1h', 0)}",
        ),
        _stage(
            "aiops-agent", "AIOps 推理",
            provider=inference.get("provider_name") or adaptive.get("mode", ""),
            ts=inference.get("inference_ts", ""),
            detail=f"{adaptive.get('mode', '')} · 复杂度 {adaptive.get('complexity_score', 0)} · 影响 {adaptive.get('impact_level', '')}",
        ),
    ]
    critique = reqs.get("hypothesis_critique") or {}
    if critique:
        stages.append(_stage(
            "hypothesis-critique", "假设评审",
            provider=critique.get("provider", ""), ts=critique.get("request_ts", ""),
        ))
    runbook_req = reqs.get("runbook_draft") or {}
    if runbook_req:
        stages.append(_stage(
            "runbook-draft", "预案生成",
            provider=runbook_req.get("provider", ""), ts=runbook_req.get("request_ts", ""),
        ))
    return stages


def _timeline(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered event dots from the record's real timestamps."""
    ctx = raw.get("context") or {}
    inference = raw.get("inference") or {}
    reqs = raw.get("reasoning_stage_requests") or {}
    critique = reqs.get("hypothesis_critique") or {}
    runbook_req = reqs.get("runbook_draft") or {}
    points = [
        (ctx.get("cluster_first_alert_ts"), "首个告警", "alert"),
        (ctx.get("cluster_last_alert_ts"), "簇末告警", "alert"),
        (inference.get("inference_ts"), "AIOps 推理", "inference"),
        (critique.get("request_ts"), "假设评审", "critique"),
        (runbook_req.get("request_ts"), "预案生成", "runbook"),
        (raw.get("suggestion_ts"), "建议产出", "suggestion"),
    ]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ts, label, kind in points:
        if not ts or ts in seen:
            continue
        seen.add(ts)
        out.append({"ts": ts, "label": label, "kind": kind})
    out.sort(key=lambda p: p["ts"])
    return out


def _hypothesis_set(raw: dict[str, Any]) -> dict[str, Any]:
    hs = raw.get("hypothesis_set") or {}
    items = [
        {
            "id": it.get("hypothesis_id", ""),
            "rank": it.get("rank", 0),
            "statement": it.get("statement", ""),
            "confidence": it.get("confidence_score", 0.0),
            "confidenceLabel": it.get("confidence_label", ""),
            "evidenceRefs": list(it.get("support_evidence_refs") or []),
        }
        for it in (hs.get("items") or [])
    ]
    return {
        "setId": hs.get("set_id", ""),
        "primaryHypothesisId": hs.get("primary_hypothesis_id", ""),
        "items": items,
        "summary": hs.get("summary") or {},
    }


def _runbook_draft(raw: dict[str, Any]) -> dict[str, Any]:
    rb = raw.get("runbook_draft") or {}
    verdict = raw.get("review_verdict") or {}
    return {
        "planId": rb.get("plan_id", ""),
        "title": rb.get("title", ""),
        "planStatus": rb.get("plan_status", ""),
        "applicability": rb.get("applicability") or {},
        "actions": list(raw.get("recommended_actions") or []),
        # NetOps never auto-executes a runbook — every AI-drafted plan is human-gated
        # before it can touch a device. approvalRequired is that safety invariant, not
        # a per-record toggle; the reviewer's own flag is kept alongside it.
        "approvalBoundary": {
            "approvalRequired": True,
            "disposition": verdict.get("recommended_disposition", ""),
            "reviewerApprovalFlag": bool(verdict.get("approval_required", False)),
        },
    }


def _review_verdict(raw: dict[str, Any]) -> dict[str, Any]:
    verdict = raw.get("review_verdict") or {}
    approval_required = bool(verdict.get("approval_required", False))
    # overreachRisk = does executing this suggestion risk overreaching? A verdict that
    # demands approval is one the gate holds back, so its status is "gated"; otherwise
    # it carries the reviewer's disposition. Derived from the real flag, never invented.
    status = "gated" if approval_required else (verdict.get("recommended_disposition") or "reviewed")
    return {
        "verdictId": verdict.get("verdict_id", ""),
        "verdictStatus": verdict.get("verdict_status", ""),
        "recommendedDisposition": verdict.get("recommended_disposition", ""),
        "checks": {
            "overreachRisk": {
                "status": status,
                "approvalRequired": approval_required,
            },
        },
    }


def _map_suggestion(raw: dict[str, Any]) -> dict[str, Any]:
    ctx = raw.get("context") or {}
    adaptive = raw.get("adaptive_analysis") or {}
    return {
        "id": raw.get("suggestion_id", ""),
        "ts": raw.get("suggestion_ts", ""),
        "scope": raw.get("suggestion_scope", ""),
        "severity": raw.get("severity", ""),
        "priority": raw.get("priority", ""),
        "summary": raw.get("summary", ""),
        "service": ctx.get("service", ""),
        "device": ctx.get("src_device_key", ""),
        "clusterSize": ctx.get("cluster_size", 0),
        "adaptiveMode": adaptive.get("mode", ""),
        "triggerReasons": list(adaptive.get("trigger_reasons") or []),
        "impactLevel": adaptive.get("impact_level", ""),
        "timeline": _timeline(raw),
        "stageTelemetry": _stage_telemetry(raw),
        "hypothesisSet": _hypothesis_set(raw),
        "runbookDraft": _runbook_draft(raw),
        "reviewVerdict": _review_verdict(raw),
    }


def _cluster_watch(runtime_dir: Path) -> list[dict[str, Any]]:
    """Rolling correlation windows from cluster-state.json, as progress toward a cluster."""
    path = runtime_dir / "aiops" / "cluster-state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for tl in state.get("timeline", []):
        key = tl.get("key") or {}
        events = tl.get("events") or []
        out.append({
            "key": f"{key.get('service', '')}·{key.get('src_device_key', '')}",
            "severity": key.get("severity", ""),
            "ruleId": key.get("rule_id", ""),
            "progress": len(events),
            "target": max(3, len(events)),
            "lastEmitTs": tl.get("last_emit_ts", ""),
        })
    return out


def load_runtime_snapshot(settings: Settings) -> dict[str, Any]:
    """A read-only snapshot of the NetOps live pipeline: feed, clusters, suggestions.

    Returns empty collections (never raises) when the NetOps runtime dir is absent,
    so the gateway degrades to "no live data" instead of failing the page.
    """
    runtime_dir = settings.netops_runtime_dir
    alerts_dir = runtime_dir / "alerts"
    aiops_dir = runtime_dir / "aiops"

    alert_file = _latest_file(alerts_dir, "alerts-")
    suggestion_file = _latest_file(aiops_dir, "suggestions-")

    alerts = _tail_records(alert_file, _ALERT_FEED) if alert_file else []
    raw_suggestions = _tail_records(suggestion_file, _SUGGESTION_FEED) if suggestion_file else []
    suggestions = [_map_suggestion(s) for s in raw_suggestions]
    # newest first, so the feed's top item and the default-selected detail agree
    suggestions.sort(key=lambda s: s["ts"], reverse=True)

    feed: list[dict[str, Any]] = []
    for a in alerts:
        feed.append({
            "id": f"feed-alert-{a.get('alert_id', '')}",
            "kind": "alert",
            "ts": a.get("alert_ts", ""),
            "severity": a.get("severity", ""),
            "device": a.get("src_device_key", ""),
            "ruleId": a.get("rule_id", ""),
            "scenario": (a.get("dimensions") or {}).get("fault_scenario", ""),
        })
    for s in suggestions:
        feed.append({
            "id": f"feed-suggestion-{s['id']}",
            "kind": "suggestion",
            "scope": s["scope"],
            "ts": s["ts"],
            "severity": s["severity"],
            "priority": s["priority"],
            "device": s["device"],
            "summary": s["summary"],
        })
    feed.sort(key=lambda f: f.get("ts", ""), reverse=True)

    latest_alert_ts = max((a.get("alert_ts", "") for a in alerts), default="n/a") or "n/a"
    latest_suggestion_ts = max((s["ts"] for s in suggestions), default="n/a") or "n/a"

    return {
        "ready": bool(suggestions or alerts),
        "feed": feed,
        "clusterWatch": _cluster_watch(runtime_dir),
        "suggestions": suggestions,
        "runtime": {
            "latestAlertTs": latest_alert_ts,
            "latestSuggestionTs": latest_suggestion_ts,
            "windowSec": 600,
        },
        "defaultSuggestionId": suggestions[0]["id"] if suggestions else "",
    }


def _feed_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item.get("id", ""): item for item in snapshot.get("feed", [])}


def build_runtime_stream_delta(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any] | None:
    """What changed between two snapshots, as a stage-path the frontend can animate.

    Returns None when nothing moved. A new alert lights the ingest path
    (correlator → alerts-topic → cluster-window); a new cluster-scoped suggestion
    lights the remediation path (cluster-window → aiops-agent → suggestions-topic →
    remediation).
    """
    prev_ids = set(_feed_index(previous))
    new_items = [item for item in current.get("feed", []) if item.get("id", "") not in prev_ids]
    if not new_items:
        return None

    newest = new_items[0]
    feed_ids = [item["id"] for item in new_items]
    kind = newest.get("kind")
    if kind == "suggestion" and newest.get("scope") == "cluster":
        return {
            "kind": "cluster",
            "reason": "feed",
            "feedIds": feed_ids,
            "stageIds": list(_SUGGESTION_PATH),
        }
    if kind == "suggestion":
        return {
            "kind": "suggestion",
            "reason": "feed",
            "feedIds": feed_ids,
            "stageIds": ["aiops-agent", "suggestions-topic", "remediation"],
        }
    return {
        "kind": "alert",
        "reason": "feed",
        "feedIds": feed_ids,
        "stageIds": list(_ALERT_PATH),
    }
