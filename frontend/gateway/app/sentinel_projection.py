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

from domains.network_rca.incident_memory import incident_ref

# Events that belong to one subject's chain. `cycle` and `sentinel_started` are
# loop bookkeeping with no subject and are skipped.
_CHAIN_KINDS = frozenset({
    "detected", "awaiting_confirmation", "no_safe_action", "cooldown",
    "preflight", "declined", "remediated", "resolved", "escalated",
    "escalation_cleared", "remediation_committed", "bakein_opened",
    "bakein_sampled", "bakein_passed", "bakein_regressed",
    "remediation_reverted", "revert_unverified",
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
    "remediation_committed": ("动作已提交", "action committed"),
    "bakein_opened": ("观察窗口开启", "observation opened"),
    "bakein_sampled": ("健康回读采样", "health readback sampled"),
    "bakein_passed": ("观察窗口通过", "observation passed"),
    "bakein_regressed": ("观察窗口发现回归", "observation regressed"),
    "remediation_reverted": ("动作已回滚", "action reverted"),
    "revert_unverified": ("回滚待验证", "revert awaiting verification"),
    "remediated": ("已执行并完成观察期", "acted, watch window closed"),
    "resolved": ("判定恢复", "resolved"),
    "no_safe_action": ("自动执行条件未满足，转人工处置", "automatic controls not satisfied — operator queued"),
    "declined": ("前置条件不通过，拒绝执行", "preconditions failed — declined"),
    "cooldown": ("冷却中", "cooling down"),
    "escalated": ("复发预算已用尽，升级人工处置", "RECURRENCE BUDGET EXHAUSTED, ESCALATED"),
    "escalation_cleared": ("目标恢复，升级解除", "TARGET RECOVERED, ESCALATION LIFTED"),
}

_ACTION_LABEL: dict[str, tuple[str, str]] = {
    "restart_unit": ("重启该 systemd 单元", "restart the systemd unit"),
    "bounce_interface": ("down/up 该网口", "bounce the interface"),
    "gateway_probe": ("探测网关连通性", "probe the gateway"),
}

_CANDIDATE_ACTION_LABEL: dict[str, tuple[str, str]] = {
    "temporary_firewall_block": ("临时防火墙封禁", "temporary firewall block"),
    "restart_unit": ("重启该 systemd 单元", "restart the systemd unit"),
}


_HOST_ADDRESS: list[str | None] = []


def host_address() -> str | None:
    """This host's own LAN address, measured once per process.

    Every detector currently in the sentinel watches *this* machine — its units,
    its links, its SSH. So the topology node an incident concerns is this host,
    and the theater needs its address to put a ring around the right dot instead
    of drawing the chain into nothing.
    """
    if _HOST_ADDRESS:
        return _HOST_ADDRESS[0]

    from core.investigate.safe_exec import run as safe_run
    from core.net.addr import is_private

    found: str | None = None
    result = safe_run("ip -br addr show")
    for line in (result.output or "").splitlines():
        fields = line.split()
        # "eth2  UP  192.168.1.27/24" — only a carrier-bearing link speaks for the host
        if len(fields) < 3 or fields[1].upper() != "UP":
            continue
        for token in fields[2:]:
            address = token.split("/")[0]
            if ":" in address or address.startswith("127."):
                continue
            if is_private(address):
                found = address
                break
        if found:
            break
    _HOST_ADDRESS.append(found)
    return found


def _is_ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _no_action_reason(
    subject: str,
    detection: dict[str, Any],
    no_action: dict[str, Any],
    *,
    en: bool,
) -> str:
    """Project the unmet action controls, including older timeline entries."""
    recorded = str(no_action.get("reason") or "").strip()
    legacy_or_demo = any(marker in recorded for marker in (
        "没有可自动执行的动作", "只出告警", "RFC 5737", "演示保留地址", "注入",
    ))
    if recorded and not legacy_or_demo:
        return recorded
    if str(detection.get("detector") or "") == "admin_bruteforce":
        return (
            f"Automatic block controls not satisfied for {subject}: source ownership and management-address "
            "exemptions are unverified, and the firewall action does not yet combine a TTL, post-commit readback, "
            "and timed rollback. Existing configuration is retained and the incident is queued for an operator."
            if en else
            f"自动封禁条件未满足：来源 {subject} 尚未完成归属确认与管理地址豁免校验；当前防火墙动作未同时提供"
            "封禁 TTL、提交后回读和超时自动回滚。策略保持现有配置，并将事件转入人工处置队列。"
        )
    return str(no_action.get("note") or (
        "No registered action passed the safety gate; state was retained and handed off."
        if en else "当前没有通过安全门的预注册动作；写操作保持不变，事件证据已记录并转人工。"
    ))


def _stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _age_seconds(iso: str, now: float) -> float:
    from datetime import datetime
    try:
        normalized = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return now - datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return float("inf")


def _last(chain: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for event in reversed(chain):
        if event.get("kind") == kind:
            return event
    return None


def _latest_cycle(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slice one subject's append-only history to its current incident cycle.

    A detection after a closed decision starts a new cycle.  Escalation stays
    sticky while the detector keeps firing: those later detections belong to
    the same incident the safety policy refused to touch.
    """
    start = 0
    next_detection_starts_cycle = False
    for index, event in enumerate(chain):
        kind = str(event.get("kind") or "")
        if kind == "detected" and next_detection_starts_cycle:
            start = index
            next_detection_starts_cycle = False
        if (
            kind in {"resolved", "no_safe_action", "declined", "cooldown", "escalation_cleared"}
            or (kind == "remediated" and event.get("needs_human") is True)
        ):
            next_detection_starts_cycle = True
    return chain[start:]


def _outcome(chain: list[dict[str, Any]]) -> tuple[str, str, bool]:
    """(verdict_status, disposition, still_running) for this subject's chain."""
    remediated = _last(chain, "remediated")
    # First, and above every other terminal: a chain that escalated contains the
    # *previous* cycles' `remediated` and `resolved` events, because those cycles
    # are what caused the escalation. Reading the last success would report this
    # subject as healed at the exact moment the system refused to touch it again.
    escalated = _last(chain, "escalated")
    if escalated is not None:
        # ...unless a person already dealt with it. The loop records that the
        # condition stopped firing; without honouring it here the card would
        # stay "needs a person" forever after the person had been.
        cleared = _last(chain, "escalation_cleared")
        if cleared is None or str(cleared.get("at", "")) < str(escalated.get("at", "")):
            return "escalated", "needs_human", False
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
    if _last(chain, "bakein_opened"):
        return "in_flight", "observing", True
    if _last(chain, "remediation_committed"):
        return "in_flight", "acting", True
    return "in_flight", "prechecking" if _last(chain, "preflight") else "confirming", True


def _card(subject: str, chain: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    en = lang == "en"
    detection = _last(chain, "detected") or {}
    preflight = _last(chain, "preflight")
    remediated = _last(chain, "remediated")
    resolved = _last(chain, "resolved")
    no_action = _last(chain, "no_safe_action")
    declined = _last(chain, "declined")
    cooldown = _last(chain, "cooldown")
    escalated = _last(chain, "escalated")
    committed = _last(chain, "remediation_committed")
    observation = _last(chain, "bakein_opened")
    sampled = [event for event in chain if event.get("kind") == "bakein_sampled"]
    no_action_reason = _no_action_reason(subject, detection, no_action, en=en) if no_action else ""
    candidate_action = str(
        (no_action or {}).get("candidate_action")
        or detection.get("candidate_action")
        or ("temporary_firewall_block" if detection.get("detector") == "admin_bruteforce" else "")
    )
    candidate_action_text = _CANDIDATE_ACTION_LABEL.get(
        candidate_action, (candidate_action, candidate_action),
    )[1 if en else 0]
    radius = (preflight or {}).get("blast_radius") or {}
    verdict_status, disposition, running = _outcome(chain)
    action = str(detection.get("action") or (preflight or {}).get("action") or "")
    last_ts = str(chain[-1].get("at") or "")

    # Both branches where the system stopped short of acting on its own. They are
    # different findings — nothing safe to run vs. this has been fixed too often
    # already — but they gate the card identically: a person has to decide.
    gated = bool(no_action) or bool(escalated)

    severity = str(detection.get("severity") or "high")
    if verdict_status in {"escalated", "needs_human"}:
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
    if committed:
        stages.append({
            "stageId": "act",
            "label": "动作提交" if not en else "action commit",
            "provider": "safe-exec",
            "ts": str(committed.get("at") or ""),
            "detail": (
                f"{action or committed.get('action') or 'registered action'} 已提交，动作回执已记录"
                if not en else
                f"{action or committed.get('action') or 'registered action'} committed; action receipt recorded"
            ),
        })
    if observation and not remediated:
        latest_sample = sampled[-1] if sampled else observation
        phase = str(latest_sample.get("phase") or "fast")
        phase_label = {
            "baseline": ("基线", "baseline"),
            "fast": ("快速回退窗口", "fast rollback window"),
            "stability": ("稳定性窗口", "stability window"),
        }.get(phase, (phase, phase))
        stages.append({
            "stageId": "watch",
            "label": "处置后观察" if not en else "post-action observation",
            "provider": "follow-up",
            "ts": str(latest_sample.get("at") or observation.get("at") or ""),
            "detail": (
                f"{phase_label[0]} · 已完成 {len(sampled)} 次健康回读"
                if not en else f"{phase_label[1]} · {len(sampled)} health readbacks completed"
            ),
        })
    if remediated:
        samples = remediated.get("samples")
        watch = (
            f"观察期 {samples} 次采样 · {remediated.get('detail', '')}"
            if not en else f"watch window, {samples} readings · {remediated.get('detail', '')}"
        )
        stages.append({
            "stageId": "watch",
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
            "label": "防火墙写入安全门" if candidate_action == "temporary_firewall_block" and not en else (
                "firewall write safety gate" if candidate_action == "temporary_firewall_block" else
                ("自动化分级闸门" if not en else "automation gate")
            ),
            "provider": "remediation-policy",
            "ts": str(no_action.get("at") or ""),
            "detail": no_action_reason,
        })
    if declined:
        stages.append({
            "stageId": "gate",
            "label": "前置校验拒绝" if not en else "preflight declined",
            "provider": "remediation-policy",
            "ts": str(declined.get("at") or ""),
            "detail": str(declined.get("reason") or declined.get("note") or ""),
        })
    if cooldown:
        remaining = int(cooldown.get("remaining_sec") or 0)
        stages.append({
            "stageId": "cooldown",
            "label": "处置安全冷却" if not en else "remediation cooldown",
            "provider": "remediation-budget",
            "ts": str(cooldown.get("at") or ""),
            "detail": (
                f"同一目标暂不重复执行，剩余 {remaining} 秒"
                if not en else f"repeat action held for {remaining} seconds"
            ),
        })
    if escalated:
        stages.append({
            "stageId": "escalated",
            "label": "复发次数已到上限" if not en else "recurrence limit reached",
            # Not a detector and not a model: the count is an aggregation over the
            # same append-only timeline this card is projected from.
            "provider": "recurrence",
            "ts": str(escalated.get("at") or ""),
            "detail": str(escalated.get("reason") or ""),
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
        action_text = "自动执行条件未满足" if not en else "automatic execution controls not satisfied"
    actions = [action_text]
    if no_action and candidate_action_text:
        actions.append(
            f"候选动作：{candidate_action_text}（未执行）"
            if not en else f"candidate action: {candidate_action_text} (not executed)"
        )
    if radius.get("summary"):
        actions.append(str(radius["summary"]))
    if no_action:
        actions.append(no_action_reason)
    if declined:
        actions.append(str(declined.get("reason") or ""))
    if cooldown:
        actions.append(
            f"处置处于安全冷却，剩余 {int(cooldown.get('remaining_sec') or 0)} 秒"
            if not en else f"remediation cooldown, {int(cooldown.get('remaining_sec') or 0)} seconds left"
        )
    if escalated:
        # Without this the runbook still reads "restart the unit" on a card whose
        # whole point is that the restart was refused.
        actions.append(str(escalated.get("reason") or ""))

    # The citation chain: which recorded fix-then-break rounds produced the
    # refusal. Carried on the card so the surfaces that draw it never have to
    # re-derive the count — and so "why the third time?" is answerable from the
    # same record the operator is already looking at.
    prior_cycles = [
        {
            "at": str(cycle.get("at") or ""),
            "outcome": str(cycle.get("outcome") or ""),
            "samples": int(cycle.get("samples") or 0),
        }
        for cycle in ((escalated or {}).get("prior_cycles") or [])
        if isinstance(cycle, dict)
    ]

    return {
        "id": f"sentinel-{_stable_id(subject)[:16]}",
        "incidentRef": incident_ref(chain) if any(
            row.get("kind") == "detected" for row in chain
        ) else None,
        "detectedAt": str(detection.get("at") or ""),
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
        # Where this sits in the topology. Every detector watches this host, so the
        # node to ring is this host — even for a bruteforce, where the *source* is
        # elsewhere but the thing under attack is here. originIp keeps that source
        # rather than silently dropping it.
        "anchorIp": host_address(),
        "originIp": subject if _is_ipv4(subject) else None,
        "clusterSize": 1,
        "adaptiveMode": "自动巡检（无人值守）" if not en else "unattended sweep",
        "triggerReasons": [r for r in reasons if r],
        "impactLevel": str(radius.get("scope") or ("待评估" if not en else "not yet measured")),
        "timeline": timeline,
        "stageTelemetry": stages,
        # Empty and zero on every card that never escalated, so a consumer can read
        # these without first asking what kind of chain it is holding.
        "priorCycles": prior_cycles,
        "recurrences": int((escalated or {}).get("recurrences") or 0),
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
                f"{subject} · 来源隔离决策" if no_action and not en
                else f"source-isolation decision for {subject}" if no_action
                else f"{subject} · {action_text}" if not en
                else f"{action_text} on {subject}"
            ),
            # `escalated` is read before `remediated`, because the successful
            # remediations still in this chain are the ones being escalated over.
            "planStatus": (
                "blocked" if escalated
                else "executed" if remediated
                else "blocked" if no_action or declined or cooldown
                else "in_flight"
            ),
            "applicability": {
                "rule_id": str(detection.get("detector") or ""),
                "service": subject,
                "family": str(detection.get("family") or ""),
            },
            "actions": [a for a in actions if a],
            "approvalBoundary": {
                # Honest per-record value: the sentinel executes unattended inside a
                # closed allowlist, so this is not the NetOps blanket True.
                "approvalRequired": gated or verdict_status == "needs_human",
                "disposition": disposition,
                "reviewerApprovalFlag": gated,
            },
        },
        "reviewVerdict": {
            "verdictId": _stable_id(subject, "verdict", last_ts),
            "verdictStatus": verdict_status,
            "recommendedDisposition": disposition,
            "checks": {
                "overreachRisk": {
                    "status": "gated" if gated else ("running" if running else "cleared"),
                    "approvalRequired": gated,
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
        _card(subject, _latest_cycle(chain), lang)
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
