"""Business decisions produced from one live investigation case.

The operator-facing object is deliberately smaller than an investigation trace.
It answers five questions: what happened, which managed asset is affected, which
current observations support that answer, what the system decided to do, and
what observation is still missing when the case cannot be closed.

Model prose, retrieval scores, and pipeline stage names are excluded from this
contract.  They may help an investigation choose a probe, but they are not a
business result.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence


DecisionState = Literal[
    "investigating",
    "action_ready",
    "observing",
    "resolved",
    "escalated",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_ip(value: str | None) -> bool:
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    evidence_id: str
    label: str
    value: str
    source: str
    observed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "observedAt": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class MissingObservation:
    code: str
    question: str
    probe: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "question": self.question, "probe": self.probe}


@dataclass(frozen=True, slots=True)
class BusinessDecision:
    case_id: str
    session_id: str
    state: DecisionState
    classification: str
    headline: str
    summary: str
    disposition: str
    action: str
    service: str = ""
    impacted_assets: tuple[str, ...] = ()
    evidence: tuple[DecisionEvidence, ...] = ()
    missing_observations: tuple[MissingObservation, ...] = ()
    next_probe: str | None = None
    readback: Mapping[str, Any] | None = None
    generated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "sessionId": self.session_id,
            "state": self.state,
            "classification": self.classification,
            "headline": self.headline,
            "summary": self.summary,
            "disposition": self.disposition,
            "action": self.action,
            "service": self.service,
            "impactedAssets": list(self.impacted_assets),
            "evidence": [item.as_dict() for item in self.evidence],
            "missingObservations": [item.as_dict() for item in self.missing_observations],
            "nextProbe": self.next_probe,
            "readback": dict(self.readback) if self.readback is not None else None,
            "generatedAt": self.generated_at,
        }


def is_terminal_local_in_deny(facts: Mapping[str, Any]) -> bool:
    """Return whether the event already proves an external probe was blocked.

    FortiGate local-in traffic is addressed to the firewall interface itself.
    A public source, WAN ingress, local-in policy type and deny action therefore
    describe a completed protection outcome.  A missing egress interface is not
    interpreted as a forwarding-path failure for this traffic class.
    """
    return (
        str(facts.get("dataClassification") or "observed") == "observed"
        and _public_ip(str(facts.get("sourceIp") or ""))
        and str(facts.get("sourceInterfaceRole") or "").casefold() == "wan"
        and str(facts.get("trafficSubtype") or "").casefold() == "local"
        and str(facts.get("policyType") or "").casefold() == "local-in-policy"
        and str(facts.get("action") or "").casefold() == "deny"
    )


def local_in_deny_decision(
    *,
    case_id: str,
    session_id: str,
    facts: Mapping[str, Any],
    evidence_id: str,
    managed_gateway: str,
) -> BusinessDecision:
    """Close a blocked external probe with a compact, evidence-backed answer."""
    source = str(facts.get("sourceIp") or "未知来源")
    destination = str(facts.get("destinationIp") or managed_gateway)
    service = str(facts.get("service") or "未知服务")
    ingress = str(facts.get("sourceInterface") or "WAN")
    count = _integer(facts.get("denyCount"))
    window = _integer(facts.get("windowSeconds"))
    volume = (
        f"{count} 次/{window} 秒"
        if count is not None and window is not None
        else "已达到拒绝突增规则阈值"
    )
    observed_at = str(facts.get("observedAt") or "") or None
    evidence = (
        DecisionEvidence(
            evidence_id=evidence_id,
            label="流量方向",
            value=f"公网来源 {source} 经 {ingress} 访问防火墙本机 {destination}:{service}",
            source="FortiGate local traffic log",
            observed_at=observed_at,
        ),
        DecisionEvidence(
            evidence_id=evidence_id,
            label="设备处理结果",
            value=f"local-in-policy 已拒绝，{volume}",
            source="FortiGate local traffic log",
            observed_at=observed_at,
        ),
        DecisionEvidence(
            evidence_id=evidence_id,
            label="影响边界",
            value="记录指向防火墙接口自身，没有经过内网转发链路",
            source="FortiGate traffic subtype and policy type",
            observed_at=observed_at,
        ),
    )
    return BusinessDecision(
        case_id=case_id,
        session_id=session_id,
        state="resolved",
        classification="blocked_external_probe",
        headline="外部探测已被防火墙拒绝",
        summary=(
            f"{source} 对 {destination} 的 {service} 发起 {volume} 访问，"
            "设备按本机入站策略拒绝。当前记录没有显示内网业务中断或策略放行失败。"
        ),
        disposition="关闭本次案件；同一目标端口出现多来源持续增长或产生放行流量时重新开案。",
        action="保持现有拒绝策略，不创建放行规则。",
        service=service,
        impacted_assets=(managed_gateway,),
        evidence=evidence,
    )


def pending_policy_decision(
    *,
    case_id: str,
    session_id: str,
    facts: Mapping[str, Any],
    evidence_id: str,
    managed_gateway: str,
    completed_probes: Sequence[str] = (),
) -> BusinessDecision:
    """Keep a policy case open with an exact missing observation and next probe."""
    source = str(facts.get("sourceIp") or "未知来源")
    destination = str(facts.get("destinationIp") or "未知目标")
    service = str(facts.get("service") or "未知服务")
    evidence = (
        DecisionEvidence(
            evidence_id=evidence_id,
            label="触发流量",
            value=f"{source} -> {destination} · {service} · {facts.get('action') or '未知动作'}",
            source="FortiGate traffic log",
            observed_at=str(facts.get("observedAt") or "") or None,
        ),
    )
    next_probe = next(
        (
            probe
            for probe in ("adapter:case_flow_window", "adapter:fortigate_context")
            if probe not in set(completed_probes)
        ),
        None,
    )
    missing = []
    if "adapter:case_flow_window" not in completed_probes:
        missing.append(MissingObservation(
            code="exact_flow_window_missing",
            question="事件窗口内是否存在同一五元组的放行、拒绝和策略变化？",
            probe="adapter:case_flow_window",
        ))
    if "adapter:fortigate_context" not in completed_probes:
        missing.append(MissingObservation(
            code="current_policy_state_missing",
            question="当前设备上的接口与策略是否匹配该目标和服务？",
            probe="adapter:fortigate_context",
        ))
    if not missing:
        missing.append(MissingObservation(
            code="policy_intent_missing",
            question="该来源、目标和服务在业务上应当放行还是拒绝？",
            probe=None,
        ))
    return BusinessDecision(
        case_id=case_id,
        session_id=session_id,
        state="investigating",
        classification="policy_outcome_unresolved",
        headline="流量已触发策略告警，处置条件仍缺失",
        summary="现有记录能够确认告警流量，尚不能区分预期阻断、策略配置错误和路径归一化缺失。",
        disposition="保持案件调查中，自动采集缺失的流量窗口和设备策略状态。",
        action="当前不执行配置变更。",
        service=service,
        impacted_assets=tuple(value for value in (managed_gateway, destination) if value),
        evidence=evidence,
        missing_observations=tuple(missing),
        next_probe=next_probe,
    )


__all__ = [
    "BusinessDecision",
    "DecisionEvidence",
    "MissingObservation",
    "is_terminal_local_in_deny",
    "local_in_deny_decision",
    "pending_policy_decision",
]
