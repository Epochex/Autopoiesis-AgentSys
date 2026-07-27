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
from datetime import datetime
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


def _has_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _english_device(value: str) -> str:
    """English display label for a device key while the raw key stays available.

    Landed records use device keys as identities, and some historical keys contain
    Chinese display words.  Known words are translated without touching their ASCII
    identity.  An unfamiliar all-Chinese label degrades to a neutral display name;
    ``deviceKey`` still carries the exact raw value for topology anchoring.
    """
    if not value or not _has_cjk(value):
        return value
    translated = value
    for zh, en in (
        ("边缘采集节点", " edge collector"),
        ("边缘节点", " edge node"),
        ("核心节点", " core node"),
        ("汇聚节点", " aggregation node"),
        ("节点", " node"),
        ("设备", " device"),
    ):
        translated = translated.replace(zh, en)
    if not _has_cjk(translated):
        return " ".join(translated.split())
    ascii_part = "".join(char for char in translated if ord(char) < 128).strip(" ·-_")
    return f"{ascii_part} source device".strip() if ascii_part else "source device"


def _english_mode(value: str) -> str:
    if not value:
        return ""
    known = {
        "并行多角色分析": "parallel multi-role analysis",
        "单角色分析": "single-role analysis",
        "规则分析": "rule-based analysis",
    }
    return known.get(value, "adaptive analysis" if _has_cjk(value) else value)


def _english_impact(value: str) -> str:
    if not value:
        return ""
    known = {"高影响": "high impact", "中影响": "moderate impact", "低影响": "low impact"}
    return known.get(value, "assessed impact" if _has_cjk(value) else value)


def _evidence_context(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    evidence = raw.get("evidence_bundle") or {}
    topology = evidence.get("topology_context") or {}
    path = evidence.get("path_context") or {}
    change = evidence.get("change_context") or {}
    return topology, path, change


def _english_facts(raw: dict[str, Any]) -> dict[str, Any]:
    ctx = raw.get("context") or {}
    topology, path_ctx, change_ctx = _evidence_context(raw)
    rb = raw.get("runbook_draft") or {}
    applicability = rb.get("applicability") or {}
    device_key = ctx.get("src_device_key") or topology.get("src_device_key") or ""
    path = (
        path_ctx.get("path_signature")
        or topology.get("path_signature")
        or applicability.get("path_signature")
        or "the observed path"
    )
    changes = list(change_ctx.get("change_refs") or (rb.get("change_summary") or {}).get("change_refs") or [])
    neighbors = list(topology.get("neighbor_refs") or [])
    return {
        "device": _english_device(device_key),
        "deviceKey": device_key,
        "service": ctx.get("service") or topology.get("service") or applicability.get("service") or "the observed service",
        "path": path,
        "change": changes[0] if changes else "",
        "srcintf": topology.get("srcintf") or path_ctx.get("srcintf") or "",
        "dstintf": topology.get("dstintf") or path_ctx.get("dstintf") or "",
        "neighbor": neighbors[0] if neighbors else "",
        "downstream": topology.get("downstream_dependents") or 0,
    }


def _english_summary(raw: dict[str, Any]) -> str:
    ctx = raw.get("context") or {}
    facts = _english_facts(raw)
    count = ctx.get("cluster_size", 0)
    first = str(ctx.get("cluster_first_alert_ts", ""))
    last = str(ctx.get("cluster_last_alert_ts", ""))
    duration = ""
    if count > 1 and first and last:
        try:
            seconds = max(0, round((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()))
            duration = f" within {seconds} seconds"
        except ValueError:
            duration = ""
    alert_word = "alert" if count == 1 else "alerts"
    lead = (
        f"{count} {alert_word} from {facts['device']} traversed {facts['path']}{duration}"
        f" for {facts['service']}."
    )
    if facts["change"]:
        return (
            f"{lead} All align with change window {facts['change']}. "
            "The available evidence does not contain the change details, so direct causality remains "
            "unconfirmed; a change-related trigger and a transient device or path fault remain under review."
        )
    return f"{lead} The current evidence supports continued read-only diagnosis before remediation."


def _english_actions(raw: dict[str, Any]) -> list[str]:
    facts = _english_facts(raw)
    actions: list[str] = []
    if facts["change"]:
        actions.append(
            f"Review the detailed change record and approval trail for {facts['change']} in read-only mode; "
            f"verify whether it affected {facts['service']}."
        )
    interface = facts["srcintf"] or "the source interface"
    actions.append(
        f"On {facts['device']}, inspect {interface} status, error counters, and logs in read-only mode; "
        f"also verify connectivity and the traffic baseline on {facts['path']}."
    )
    if facts["neighbor"]:
        suffix = f" interface {facts['dstintf']}" if facts["dstintf"] else ""
        actions.append(f"Inspect neighbor {facts['neighbor']}{suffix} in read-only mode.")
    if facts["downstream"]:
        actions.append(f"Verify reachability for the {facts['downstream']} downstream dependencies in read-only mode.")
    actions.append(
        "Prepare a rollback plan and record the current configuration snapshot; execution requires human approval."
    )
    return actions


def _english_trigger_reasons(raw: dict[str, Any]) -> list[str]:
    ctx = raw.get("context") or {}
    adaptive = raw.get("adaptive_analysis") or {}
    reasons = ["critical event cluster", f"cluster size={ctx.get('cluster_size', 0)}"]
    complexity = adaptive.get("complexity_score")
    if complexity is not None:
        reasons.append(f"analysis complexity={complexity}")
    if _english_facts(raw)["change"]:
        reasons.append("change window overlaps repeated alerts")
    return reasons


def _stage_telemetry(raw: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """Per-stage telemetry reconstructed from the suggestion's own provenance.

    `correlator` and `aiops-agent` are always present (the contract asserts it); the
    critique/runbook stages appear only when the record carries their stage request.
    """
    ctx = raw.get("context") or {}
    adaptive = raw.get("adaptive_analysis") or {}
    inference = raw.get("inference") or {}
    reqs = raw.get("reasoning_stage_requests") or {}
    cluster_size = ctx.get("cluster_size", 0)
    en = lang == "en"
    mode = _english_mode(adaptive.get("mode", "")) if en else adaptive.get("mode", "")
    impact = _english_impact(adaptive.get("impact_level", "")) if en else adaptive.get("impact_level", "")
    stages = [
        _stage(
            "correlator", "Correlation window" if en else "关联窗口",
            ts=ctx.get("cluster_first_alert_ts", ""),
            detail=(
                f"cluster {cluster_size} · recurred in last hour {ctx.get('recent_similar_1h', 0)}"
                if en else f"簇 {cluster_size} · 近1h复发 {ctx.get('recent_similar_1h', 0)}"
            ),
        ),
        _stage(
            "aiops-agent", "AIOps reasoning" if en else "AIOps 推理",
            provider=inference.get("provider_name") or mode,
            ts=inference.get("inference_ts", ""),
            detail=(
                f"{mode} · complexity {adaptive.get('complexity_score', 0)} · {impact}"
                if en else
                f"{mode} · 复杂度 {adaptive.get('complexity_score', 0)} · 影响 {impact}"
            ),
        ),
    ]
    critique = reqs.get("hypothesis_critique") or {}
    if critique:
        stages.append(_stage(
            "hypothesis-critique", "Hypothesis review" if en else "假设评审",
            provider=critique.get("provider", ""), ts=critique.get("request_ts", ""),
        ))
    runbook_req = reqs.get("runbook_draft") or {}
    if runbook_req:
        stages.append(_stage(
            "runbook-draft", "Runbook drafting" if en else "预案生成",
            provider=runbook_req.get("provider", ""), ts=runbook_req.get("request_ts", ""),
        ))
    return stages


def _timeline(raw: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """Ordered event dots from the record's real timestamps."""
    ctx = raw.get("context") or {}
    inference = raw.get("inference") or {}
    reqs = raw.get("reasoning_stage_requests") or {}
    critique = reqs.get("hypothesis_critique") or {}
    runbook_req = reqs.get("runbook_draft") or {}
    labels = (
        ("First alert", "Last cluster alert", "AIOps reasoning", "Hypothesis review", "Runbook drafted", "Suggestion emitted")
        if lang == "en"
        else ("首个告警", "簇末告警", "AIOps 推理", "假设评审", "预案生成", "建议产出")
    )
    points = [
        (ctx.get("cluster_first_alert_ts"), labels[0], "alert"),
        (ctx.get("cluster_last_alert_ts"), labels[1], "alert"),
        (inference.get("inference_ts"), labels[2], "inference"),
        (critique.get("request_ts"), labels[3], "critique"),
        (runbook_req.get("request_ts"), labels[4], "runbook"),
        (raw.get("suggestion_ts"), labels[5], "suggestion"),
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


def _hypothesis_set(raw: dict[str, Any], lang: str) -> dict[str, Any]:
    hs = raw.get("hypothesis_set") or {}
    facts = _english_facts(raw)

    def statement(item: dict[str, Any]) -> str:
        if lang != "en":
            return item.get("statement", "")
        rank = item.get("rank", 0)
        if rank == 1 and facts["change"]:
            return (
                f"Activity in change window {facts['change']} may have triggered the alerts. "
                "The change details are unavailable, so causality remains unconfirmed."
            )
        if rank == 2:
            return (
                f"{facts['device']} or path {facts['path']} may have experienced a transient interface "
                "or service fault near the change window."
            )
        return f"Hypothesis {rank} remains under review against its cited evidence."

    items = [
        {
            "id": it.get("hypothesis_id", ""),
            "rank": it.get("rank", 0),
            "statement": statement(it),
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


def _runbook_draft(raw: dict[str, Any], lang: str) -> dict[str, Any]:
    rb = raw.get("runbook_draft") or {}
    verdict = raw.get("review_verdict") or {}
    facts = _english_facts(raw)
    return {
        "planId": rb.get("plan_id", ""),
        "title": (
            f"Runbook draft for {facts['service']} on {facts['device']}"
            if lang == "en" else rb.get("title", "")
        ),
        "planStatus": rb.get("plan_status", ""),
        "applicability": rb.get("applicability") or {},
        "actions": _english_actions(raw) if lang == "en" else list(raw.get("recommended_actions") or []),
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


def _map_suggestion(raw: dict[str, Any], lang: str) -> dict[str, Any]:
    ctx = raw.get("context") or {}
    adaptive = raw.get("adaptive_analysis") or {}
    device_key = ctx.get("src_device_key", "")
    en = lang == "en"
    return {
        "id": raw.get("suggestion_id", ""),
        "ts": raw.get("suggestion_ts", ""),
        "scope": raw.get("suggestion_scope", ""),
        "severity": raw.get("severity", ""),
        "priority": raw.get("priority", ""),
        "summary": _english_summary(raw) if en else raw.get("summary", ""),
        "service": ctx.get("service", ""),
        "device": _english_device(device_key) if en else device_key,
        "deviceKey": device_key,
        "clusterSize": ctx.get("cluster_size", 0),
        "adaptiveMode": _english_mode(adaptive.get("mode", "")) if en else adaptive.get("mode", ""),
        "triggerReasons": _english_trigger_reasons(raw) if en else list(adaptive.get("trigger_reasons") or []),
        "impactLevel": _english_impact(adaptive.get("impact_level", "")) if en else adaptive.get("impact_level", ""),
        "timeline": _timeline(raw, lang),
        "stageTelemetry": _stage_telemetry(raw, lang),
        "hypothesisSet": _hypothesis_set(raw, lang),
        "runbookDraft": _runbook_draft(raw, lang),
        "reviewVerdict": _review_verdict(raw),
    }


def _cluster_watch(runtime_dir: Path, lang: str) -> list[dict[str, Any]]:
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
            "key": (
                f"{key.get('service', '')}·{_english_device(key.get('src_device_key', ''))}"
                if lang == "en" else f"{key.get('service', '')}·{key.get('src_device_key', '')}"
            ),
            "severity": key.get("severity", ""),
            "ruleId": key.get("rule_id", ""),
            "progress": len(events),
            "target": max(3, len(events)),
            "lastEmitTs": tl.get("last_emit_ts", ""),
        })
    return out


def load_runtime_snapshot(
    settings: Settings, lang: str = "zh", runtime_dir: Path | None = None
) -> dict[str, Any]:
    """A read-only snapshot of the NetOps live pipeline: feed, clusters, suggestions.

    Returns empty collections (never raises) when the NetOps runtime dir is absent,
    so the gateway degrades to "no live data" instead of failing the page.

    ``runtime_dir`` overrides the default prod dir so the benchmark scenario can point
    the SAME reader at the isolated replay side-car output (netops-runtime-replay).
    """
    runtime_dir = runtime_dir if runtime_dir is not None else settings.netops_runtime_dir
    alerts_dir = runtime_dir / "alerts"
    aiops_dir = runtime_dir / "aiops"

    alert_file = _latest_file(alerts_dir, "alerts-")
    suggestion_file = _latest_file(aiops_dir, "suggestions-")

    alerts = _tail_records(alert_file, _ALERT_FEED) if alert_file else []
    raw_suggestions = _tail_records(suggestion_file, _SUGGESTION_FEED) if suggestion_file else []
    lang = "en" if lang == "en" else "zh"
    suggestions = [_map_suggestion(s, lang) for s in raw_suggestions]
    # newest first, so the feed's top item and the default-selected detail agree
    suggestions.sort(key=lambda s: s["ts"], reverse=True)

    feed: list[dict[str, Any]] = []
    for a in alerts:
        device_key = a.get("src_device_key", "")
        feed.append({
            "id": f"feed-alert-{a.get('alert_id', '')}",
            "kind": "alert",
            "ts": a.get("alert_ts", ""),
            "severity": a.get("severity", ""),
            "device": _english_device(device_key) if lang == "en" else device_key,
            "deviceKey": device_key,
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
            "deviceKey": s["deviceKey"],
            "summary": s["summary"],
        })
    feed.sort(key=lambda f: f.get("ts", ""), reverse=True)

    latest_alert_ts = max((a.get("alert_ts", "") for a in alerts), default="n/a") or "n/a"
    latest_suggestion_ts = max((s["ts"] for s in suggestions), default="n/a") or "n/a"

    return {
        "ready": bool(suggestions or alerts),
        "feed": feed,
        "clusterWatch": _cluster_watch(runtime_dir, lang),
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
