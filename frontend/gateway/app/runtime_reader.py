"""Read Autopoiesis event-pipeline output for the live-situation panel.

The event pipeline lands alert envelopes under ``{stream}/alerts``.  Optional
case-candidate files under ``{stream}/aiops`` remain supported for isolated
replay fixtures.  Production investigation decisions come from the durable case
service after it receives the exact incident fields.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings

# Suggestion sink files can reach 200+ MB/hour under stress load, so never json.load
# the whole file — read a bounded window off the tail and keep the last N lines.
_TAIL_BYTES = 512 * 1024
_ALERT_FEED = 12
_SUGGESTION_FEED = 8
_SINK_LOOKBACK_HOURS = 24

# The disk sink still contains the older reasoning payload.  The live business
# projection below deliberately exposes detection facts only.  Model stages,
# confidence bars and draft runbooks do not become operator-facing decisions.
_ALERT_PATH = ["correlator", "alerts-topic", "cluster-window"]
_SUGGESTION_PATH = ["cluster-window", "case-investigation"]


def _recent_files(directory: Path, prefix: str) -> list[Path]:
    """Return sink rotations within 24 hours of the newest named hour."""
    try:
        candidates = [
            p for p in directory.iterdir()
            if p.name.startswith(prefix) and p.name.endswith(".jsonl")
        ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    dated: list[tuple[datetime, Path]] = []
    for path in candidates:
        stamp = path.name.removeprefix(prefix).removesuffix(".jsonl")[:11]
        try:
            dated.append((datetime.strptime(stamp, "%Y%m%d-%H"), path))
        except ValueError:
            continue
    if dated:
        newest = max(stamp for stamp, _path in dated)
        cutoff = newest - timedelta(hours=_SINK_LOOKBACK_HOURS)
        return [path for stamp, path in sorted(dated) if stamp >= cutoff]
    return sorted(candidates, key=lambda p: p.stat().st_mtime)[-2:]


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


def _recent_records(directory: Path, prefix: str, count: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in reversed(_recent_files(directory, prefix)):
        records[:0] = _tail_records(path, count)
        if len(records) >= count:
            break
    return records[-count:]


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


def _data_classification(raw: dict[str, Any]) -> str:
    evidence = raw.get("evidence_bundle") or {}
    metrics = ((evidence.get("rule_context") or {}).get("metrics") or {})
    dataset = evidence.get("dataset_context") or raw.get("dataset_context") or {}
    event = raw.get("event_excerpt") or {}
    history = (evidence.get("historical_context") or {}).get("recent_alert_samples") or ()
    identities = [
        raw.get("source_event_id"), raw.get("alert_id"), event.get("event_id"),
        *(item.get("event_id") for item in history if isinstance(item, dict)),
    ]
    explicit_test_identity = any(
        str(value or "").casefold().startswith(
            ("controlled-", "redpanda-e2e-", "replay-", "bvaccept-", "autopoiesis-acceptance-")
        )
        for value in identities
    )
    annotated_fault = bool(
        str(raw.get("rule_id") or "") == "annotated_fault_v1"
        and str(event.get("subtype") or "") == "fault_annotation"
    )
    return (
        "controlled_test"
        if (
            metrics.get("label_field") == "controlled_test"
            or dataset.get("controlled_test")
            or explicit_test_identity
            or annotated_fault
        )
        else "observed"
    )


def _incident_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Project the exact event fields needed for a business decision.

    The earlier projection dropped ``policyid=0`` and the local traffic subtype,
    then tried to reconstruct meaning from path text.  Keeping the source fields
    intact lets the case service distinguish traffic addressed to the firewall
    from traffic forwarded through it.
    """
    evidence = raw.get("evidence_bundle") or {}
    topology = evidence.get("topology_context") or {}
    history = evidence.get("historical_context") or {}
    rule = evidence.get("rule_context") or {}
    samples = list(history.get("recent_alert_samples") or ())
    sample = samples[0] if samples and isinstance(samples[0], dict) else {}
    policy = evidence.get("policy_context") or {}
    metrics = rule.get("metrics") or {}
    policy_id = sample.get("policyid")
    if policy_id in (None, ""):
        policy_id = policy.get("policyid")
    return {
        "dataClassification": _data_classification(raw),
        "alertId": str((evidence.get("alert_ref") or {}).get("alert_id") or raw.get("alert_id") or ""),
        "observedAt": str(sample.get("alert_ts") or raw.get("suggestion_ts") or ""),
        "sourceEventId": str(sample.get("event_id") or raw.get("source_event_id") or ""),
        "sourceIp": str(sample.get("srcip") or topology.get("srcip") or ""),
        "sourcePort": sample.get("srcport") or topology.get("srcport"),
        "destinationIp": str(sample.get("dstip") or topology.get("dstip") or ""),
        "destinationPort": sample.get("dstport") or topology.get("dstport"),
        "protocol": sample.get("proto") or topology.get("proto"),
        "service": str(sample.get("service") or topology.get("service") or ""),
        "action": str(sample.get("action") or ""),
        "trafficSubtype": str(sample.get("subtype") or topology.get("subtype") or ""),
        "policyId": policy_id,
        "policyType": str(sample.get("policytype") or policy.get("policytype") or ""),
        "sourceInterface": str(sample.get("srcintf") or topology.get("srcintf") or ""),
        "sourceInterfaceRole": str(sample.get("srcintfrole") or topology.get("srcintfrole") or ""),
        "destinationInterface": str(sample.get("dstintf") or topology.get("dstintf") or ""),
        "destinationInterfaceRole": str(sample.get("dstintfrole") or topology.get("dstintfrole") or ""),
        "denyCount": metrics.get("deny_count") or sample.get("deny_count"),
        "threshold": metrics.get("threshold") or sample.get("threshold"),
        "windowSeconds": metrics.get("window_sec") or sample.get("window_sec"),
        "recentSimilar1h": history.get("recent_similar_1h"),
        "clusterSize": history.get("cluster_size"),
    }


def _alert_incident_facts(raw: dict[str, Any]) -> dict[str, Any]:
    event = raw.get("event_excerpt") or {}
    metrics = raw.get("metrics") or {}
    return {
        "dataClassification": _data_classification(raw),
        "alertId": str(raw.get("alert_id") or ""),
        "observedAt": str(raw.get("alert_ts") or event.get("event_ts") or ""),
        "sourceEventId": str(raw.get("source_event_id") or event.get("event_id") or ""),
        "sourceIp": str(event.get("srcip") or ""),
        "sourcePort": event.get("srcport"),
        "destinationIp": str(event.get("dstip") or ""),
        "destinationPort": event.get("dstport"),
        "protocol": event.get("proto"),
        "service": str(event.get("service") or ""),
        "action": str(event.get("action") or ""),
        "trafficSubtype": str(event.get("subtype") or ""),
        "policyId": event.get("policyid"),
        "policyType": str(event.get("policytype") or ""),
        "sourceInterface": str(event.get("srcintf") or ""),
        "sourceInterfaceRole": str(event.get("srcintfrole") or ""),
        "destinationInterface": str(event.get("dstintf") or ""),
        "destinationInterfaceRole": str(event.get("dstintfrole") or ""),
        "denyCount": metrics.get("deny_count"),
        "threshold": metrics.get("threshold") or metrics.get("failure_threshold"),
        "windowSeconds": metrics.get("window_sec"),
        "failedLogins": metrics.get("failed_logins"),
        "distinctSources": metrics.get("distinct_sources"),
        "distinctSourceThreshold": metrics.get("distinct_source_threshold"),
        "lockouts": metrics.get("lockouts"),
        "topSources": list(metrics.get("top_sources") or ()),
        "username": str(event.get("user") or ""),
        "authMethod": str(event.get("method") or ""),
        "authStatus": str(event.get("event_status") or ""),
        "authReason": str(event.get("reason") or ""),
        "managedDevice": str(event.get("device_key") or raw.get("src_device_key") or ""),
    }


def _detection_summary(raw: dict[str, Any], lang: str) -> str:
    facts = _incident_facts(raw)
    source = facts.get("sourceIp") or "?"
    destination = facts.get("destinationIp") or "?"
    port = facts.get("destinationPort")
    service = facts.get("service") or (f"port {port}" if port else "unknown service")
    count = facts.get("denyCount") or facts.get("clusterSize") or 0
    action = facts.get("action") or "observed"
    if lang == "en":
        return f"{count} {action} events: {source} -> {destination}, {service}."
    return f"{count} 条{action}记录：{source} → {destination}，{service}。"


def _map_suggestion(raw: dict[str, Any], lang: str) -> dict[str, Any]:
    ctx = raw.get("context") or {}
    evidence_bundle = raw.get("evidence_bundle") or {}
    dataset_context = evidence_bundle.get("dataset_context") or {}
    data_classification = _data_classification(raw)
    device_key = ctx.get("src_device_key", "")
    en = lang == "en"
    source_alert_ids = list(ctx.get("cluster_sample_alert_ids") or [])
    direct_alert_id = str(raw.get("alert_id") or "").strip()
    if direct_alert_id and direct_alert_id not in source_alert_ids:
        source_alert_ids.append(direct_alert_id)
    return {
        "id": raw.get("suggestion_id", ""),
        "ts": raw.get("suggestion_ts", ""),
        "scope": raw.get("suggestion_scope", ""),
        "ruleId": raw.get("rule_id", ""),
        "sourceAlertIds": source_alert_ids,
        "dataClassification": data_classification,
        "datasetRunId": dataset_context.get("run_id", ""),
        "severity": raw.get("severity", ""),
        "priority": raw.get("priority", ""),
        "summary": _detection_summary(raw, lang),
        "service": ctx.get("service", ""),
        "device": _english_device(device_key) if en else device_key,
        "deviceKey": device_key,
        "clusterSize": ctx.get("cluster_size", 0),
        "incidentFacts": _incident_facts(raw),
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
    """A read-only snapshot of the Autopoiesis live event feed.

    Returns empty collections (never raises) when the stream directory is absent,
    so the gateway degrades to "no live data" instead of failing the page.

    ``runtime_dir`` overrides the default prod dir so the benchmark scenario can point
    the same reader at the isolated replay side-car output.
    """
    runtime_dir = runtime_dir if runtime_dir is not None else settings.stream_output_dir
    alerts_dir = runtime_dir / "alerts"
    aiops_dir = runtime_dir / "aiops"

    alerts = _recent_records(alerts_dir, "alerts-", _ALERT_FEED)
    raw_suggestions = _recent_records(aiops_dir, "suggestions-", _SUGGESTION_FEED)
    lang = "en" if lang == "en" else "zh"
    suggestions = [_map_suggestion(s, lang) for s in raw_suggestions]
    # newest first, so the feed's top item and the default-selected detail agree
    suggestions.sort(key=lambda s: s["ts"], reverse=True)

    feed: list[dict[str, Any]] = []
    for a in alerts:
        device_key = a.get("src_device_key", "")
        event_excerpt = a.get("event_excerpt") or {}
        metrics = a.get("metrics") or {}
        feed.append({
            "id": f"feed-alert-{a.get('alert_id', '')}",
            "sourceId": a.get("alert_id", ""),
            "kind": "alert",
            "ts": a.get("alert_ts", ""),
            "severity": a.get("severity", ""),
            "device": _english_device(device_key) if lang == "en" else device_key,
            "deviceKey": device_key,
            "ruleId": a.get("rule_id", ""),
            "service": event_excerpt.get("service", ""),
            "sourceEventId": a.get("source_event_id", ""),
            "sourcePath": event_excerpt.get("source_path", ""),
            "datasetRunId": (a.get("dataset_context") or {}).get("run_id", ""),
            "dataClassification": (
                "controlled_test"
                if metrics.get("label_field") == "controlled_test"
                else "observed"
            ),
            "scenario": (a.get("dimensions") or {}).get("fault_scenario", ""),
            "incidentFacts": _alert_incident_facts(a),
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
