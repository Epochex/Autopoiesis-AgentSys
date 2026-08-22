"""Project the sentinel's own timeline into the live-situation card contract.

The situational page and the 长轨迹 card list are fed by `runtime_reader`, which
tails what the NetOps stream pipeline lands on disk. The sentinel is a different
subsystem writing a different file, so until now a fault it found, acted on and
closed left both pages completely unchanged — the operator watched the page that
matters and saw nothing happen.

This module is the join. It reads the sentinel timeline, groups the events by
subject, and emits one card per subject in exactly the shape `_map_suggestion`
produces, so the existing list, detail pane and theater render it with no
special cases.

Every field is a projection of a recorded event; nothing here invents a value.
Two places where that honesty costs a field:

  * `hypothesisSet` carries one item at confidence 1.0 — the detector matched a
    literal state (a unit is `failed`, a link has no carrier), so there is no
    ranked hypothesis set to show. Claiming a spread of candidates would be
    describing an inference the system never made.
  * `runbookDraft.approvalBoundary.approvalRequired` is False when the sentinel
    executed unattended. The NetOps mapper hardcodes True because that pipeline
    never auto-executes; the sentinel does, within a closed allowlist, and the
    card has to say so rather than inherit a claim from the other subsystem.
"""
from __future__ import annotations

import hashlib
from typing import Any

# Events that belong to one subject's chain. `cycle` and `sentinel_started` are
# loop bookkeeping with no subject and are skipped.
_CHAIN_KINDS = frozenset({
    "detected", "awaiting_confirmation", "no_safe_action", "cooldown",
    "preflight", "declined", "remediated", "resolved",
})

# Cards older than this are history the ledger already keeps; the live list is
# for what is happening now.
_MAX_AGE_SEC = 6 * 3600
_MAX_CARDS = 6
# Enough tail to hold every chain inside the age window even when the loop has
# been ticking every 15s; `timeline()` returns the last N events.
_TIMELINE_SCAN = 2000

_STEP_LABEL: dict[str, tuple[str, str]] = {
    "detected": ("发现", "detected"),
    "awaiting_confirmation": ("等待二次确认", "awaiting confirmation"),
    "preflight": ("前置校验", "preflight check"),
    "remediated": ("已执行并完成观察期", "acted, watch window closed"),
    "resolved": ("判定恢复", "resolved"),
    "no_safe_action": ("无安全动作，只报不动", "no safe action — reported only"),
    "declined": ("前置条件不通过，拒绝执行", "preconditions failed — declined"),
    "cooldown": ("冷却中", "cooling down"),
}

_ACTION_LABEL: dict[str, tuple[str, str]] = {
    "restart_unit": ("重启该 systemd 单元", "restart the systemd unit"),
    "bounce_interface": ("down/up 该网口", "bounce the interface"),
    "gateway_probe": ("探测网关连通性", "probe the gateway"),
}


def _stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _age_seconds(iso: str, now: float) -> float:
    from datetime import datetime
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return float("inf")


def _last(chain: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for event in reversed(chain):
        if event.get("kind") == kind:
            return event
    return None


def _outcome(chain: list[dict[str, Any]]) -> tuple[str, str, bool]:
    """(verdict_status, disposition, still_running) for this subject's chain."""
    remediated = _last(chain, "remediated")
    if _last(chain, "resolved"):
        return "closed", "resolved", False
    if remediated is not None:
        if remediated.get("needs_human"):
            return "needs_human", str(remediated.get("outcome") or "revert_unverified"), False
        return "closed", str(remediated.get("outcome") or "passed"), False
    if _last(chain, "no_safe_action"):
        return "reported", "no_safe_action", False
    if _last(chain, "declined"):
        return "declined", "preconditions_failed", False
    if _last(chain, "cooldown"):
        return "cooling", "cooldown", False
    return "in_flight", "acting" if _last(chain, "preflight") else "confirming", True


def _card(subject: str, chain: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    en = lang == "en"
    detection = _last(chain, "detected") or {}
    preflight = _last(chain, "preflight")
    remediated = _last(chain, "remediated")
    resolved = _last(chain, "resolved")
    no_action = _last(chain, "no_safe_action")
    radius = (preflight or {}).get("blast_radius") or {}
    verdict_status, disposition, running = _outcome(chain)
    action = str(detection.get("action") or (preflight or {}).get("action") or "")
    last_ts = str(chain[-1].get("at") or "")

    severity = str(detection.get("severity") or "high")
    if verdict_status == "needs_human":
        priority = "P1"
    elif running:
        priority = "P1" if severity == "critical" else "P2"
    elif verdict_status == "reported":
        priority = "P2"
    else:
        priority = "P3"

    timeline = [
        {
            "ts": str(event.get("at") or ""),
            "label": _STEP_LABEL.get(str(event.get("kind")), (str(event.get("kind")),) * 2)[1 if en else 0],
            "kind": str(event.get("kind")),
        }
        for event in chain
    ]

    stages: list[dict[str, Any]] = [{
        "stageId": "detector",
        "label": "巡检探测器" if not en else "detector sweep",
        "provider": str(detection.get("detector") or ""),
        "ts": str(detection.get("at") or ""),
        "detail": str((detection.get("evidence") or {}).get("line") or detection.get("summary") or ""),
    }]
    if preflight:
        stages.append({
            "stageId": "preflight",
            "label": "前置校验" if not en else "preflight",
            "provider": "blast-radius",
            "ts": str(preflight.get("at") or ""),
            "detail": str(radius.get("summary") or preflight.get("reason") or ""),
        })
    if remediated:
        samples = remediated.get("samples")
        watch = (
            f"观察期 {samples} 次采样 · {remediated.get('detail', '')}"
            if not en else f"watch window, {samples} readings · {remediated.get('detail', '')}"
        )
        stages.append({
            "stageId": "watch-window",
            "label": "处置后观察期" if not en else "post-change watch",
            "provider": "follow-up",
            "ts": str(remediated.get("at") or ""),
            "detail": watch,
        })
    if resolved:
        stages.append({
            "stageId": "verify",
            "label": "回读验证" if not en else "grounded readback",
            "provider": "safe-exec",
            "ts": str(resolved.get("at") or ""),
            "detail": str(resolved.get("note") or ""),
        })
    if no_action:
        stages.append({
            "stageId": "gate",
            "label": "自动化分级闸门" if not en else "automation gate",
            "provider": "fault-catalog",
            "ts": str(no_action.get("at") or ""),
            "detail": str(no_action.get("reason") or no_action.get("note") or ""),
        })

    statement = str(detection.get("summary") or "")
    hypothesis_id = _stable_id(subject, "hypothesis", statement)
    reasons = [str(detection.get("detector") or "")]
    evidence_line = str((detection.get("evidence") or {}).get("line") or "")
    if evidence_line:
        reasons.append(evidence_line)

    if action:
        action_text = _ACTION_LABEL.get(action, (action, action))[1 if en else 0]
    else:
        action_text = "无可自动执行的动作" if not en else "no action is auto-executable here"
    actions = [action_text]
    if radius.get("summary"):
        actions.append(str(radius["summary"]))
    if no_action:
        actions.append(str(no_action.get("reason") or ""))

    return {
        "id": f"sentinel-{_stable_id(subject)[:16]}",
        "ts": last_ts,
        "scope": "sentinel",
        "severity": severity,
        "priority": priority,
        "summary": statement,
        "service": subject,
        # deviceKey is what the theater passes to the live-progress panel, so it
        # must be the sentinel's own subject string verbatim.
        "device": subject,
        "deviceKey": subject,
        "clusterSize": 1,
        "adaptiveMode": "自动巡检（无人值守）" if not en else "unattended sweep",
        "triggerReasons": [r for r in reasons if r],
        "impactLevel": str(radius.get("scope") or ("待评估" if not en else "not yet measured")),
        "timeline": timeline,
        "stageTelemetry": stages,
        "hypothesisSet": {
            "setId": _stable_id(subject, "set"),
            "primaryHypothesisId": hypothesis_id,
            "items": [{
                "id": hypothesis_id,
                "rank": 1,
                "statement": statement,
                # A literal state match, not a ranked inference — see module docstring.
                "confidence": 1.0,
                "confidenceLabel": "实测状态" if not en else "measured state",
                "evidenceRefs": [evidence_line] if evidence_line else [],
            }],
            "summary": {"observed": 1},
        },
        "runbookDraft": {
            "planId": _stable_id(subject, action),
            "title": (
                f"{subject} · {action_text}" if not en
                else f"{action_text} on {subject}"
            ),
            "planStatus": "executed" if remediated else ("blocked" if no_action else "in_flight"),
            "applicability": {
                "rule_id": str(detection.get("detector") or ""),
                "service": subject,
                "family": str(detection.get("family") or ""),
            },
            "actions": [a for a in actions if a],
            "approvalBoundary": {
                # Honest per-record value: the sentinel executes unattended inside a
                # closed allowlist, so this is not the NetOps blanket True.
                "approvalRequired": bool(no_action) or verdict_status == "needs_human",
                "disposition": disposition,
                "reviewerApprovalFlag": bool(no_action),
            },
        },
        "reviewVerdict": {
            "verdictId": _stable_id(subject, "verdict", last_ts),
            "verdictStatus": verdict_status,
            "recommendedDisposition": disposition,
            "checks": {
                "overreachRisk": {
                    "status": "gated" if no_action else ("running" if running else "cleared"),
                    "approvalRequired": bool(no_action),
                },
            },
        },
    }


def sentinel_cards(lang: str = "zh", *, now: float | None = None) -> list[dict[str, Any]]:
    """Recent sentinel chains as live-situation cards, newest first.

    Never raises: a missing or half-written timeline yields an empty list, so the
    live-situation panel degrades to the NetOps-only view it had before.
    """
    import time

    from core.remediate.sentinel import timeline

    now = time.time() if now is None else now
    chains: dict[str, list[dict[str, Any]]] = {}
    for event in timeline(_TIMELINE_SCAN):
        if not isinstance(event, dict) or event.get("kind") not in _CHAIN_KINDS:
            continue
        subject = str(event.get("subject") or "")
        if subject:
            chains.setdefault(subject, []).append(event)

    cards = [
        _card(subject, chain, lang)
        for subject, chain in chains.items()
        if chain and _age_seconds(str(chain[-1].get("at") or ""), now) <= _MAX_AGE_SEC
    ]
    cards.sort(key=lambda c: c["ts"], reverse=True)
    return cards[:_MAX_CARDS]


def merge_into_snapshot(snapshot: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """Fold sentinel cards into a runtime snapshot, in place, and return it."""
    cards = sentinel_cards(lang)
    if not cards:
        return snapshot

    merged = cards + list(snapshot.get("suggestions") or [])
    merged.sort(key=lambda s: s.get("ts", ""), reverse=True)
    snapshot["suggestions"] = merged
    feed = list(snapshot.get("feed") or [])
    for card in cards:
        feed.append({
            "id": f"feed-suggestion-{card['id']}",
            "kind": "suggestion",
            "scope": card["scope"],
            "ts": card["ts"],
            "severity": card["severity"],
            "priority": card["priority"],
            "device": card["device"],
            "deviceKey": card["deviceKey"],
            "summary": card["summary"],
        })
    feed.sort(key=lambda f: f.get("ts", ""), reverse=True)
    snapshot["feed"] = feed
    snapshot["ready"] = True
    # The newest card wins the default selection, which after an injection is the
    # incident the operator just caused.
    snapshot["defaultSuggestionId"] = snapshot["suggestions"][0]["id"]
    return snapshot
