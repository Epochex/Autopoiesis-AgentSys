from __future__ import annotations

import json

from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


ROOT_CAUSES = {
    "carrier_down": "eno1 has no carrier because the cable is unplugged or the peer switch port is down; this is not a NIC hardware fault.",
    "benign_rx_dropped": "eno2 has high cumulative RX dropped counters with zero hardware errors, consistent with broadcast or multicast pressure or historical accumulation; keep observing instead of replacing hardware.",
    "fg_policy_missing": "Showroom to office reachability fails at FortiGate layer-3 policy or address-object matching, not at layer 2.",
    "security_subscription_expired_forwarding_ok": "Expired FortiGuard AV, IPS, and WebFilter subscriptions reduce security inspection coverage but do not block basic forwarding.",
    "vip_policy_mismatch": "The WAN VIP port mapping is inconsistent with the matching firewall policy or service object, so the specific published service is not reachable.",
    "admin_bruteforce_lockout": "Repeated external admin login failures from many source IPs triggered FortiGate admin-login lockout; this is an exposure/attack pattern on the management interface, not a device fault.",
    "internal_policy_deny_expected": "The high deny volume is internal hosts hitting blocked ports (NetBIOS and similar) and is expected firewall policy behaviour, not an outage or misconfiguration.",
    "benign_session_clash": "Session-clash and update events are informational FortiGate housekeeping logs and do not indicate a fault.",
    "dhcp_service_healthy": "DHCP ACK and statistics events confirm the FortiGate is issuing leases normally; address allocation is healthy.",
    "security_posture_current": "Successful FortiGuard updates and security-rating summaries confirm the security posture is current.",
    "device_service_port_probe_contained": "Denied probes against camera/DVR service ports (37777/37809/37810) confirm firewall policy is containing device-service exposure.",
    "firewall_resource_healthy": "Low CPU, low memory and a small session table confirm the firewall has ample headroom.",
}

ROOT_CAUSE_EVIDENCE_CONTRACTS: dict[str, set[str]] = {
    "carrier_down": {"ev-eno1-oper-down", "ev-eno1-no-phy"},
    "benign_rx_dropped": {"ev-eno2-dropped", "ev-eno2-no-hw-errors"},
    "fg_policy_missing": {"ev-fg-connected-routes", "ev-fg-policy-deny"},
    "security_subscription_expired_forwarding_ok": {
        "ev-fortiguard-expired", "ev-forwarding-ok",
    },
    "vip_policy_mismatch": {"ev-vip-map-8443", "ev-vip-policy-service-mismatch"},
    "admin_bruteforce_lockout": {"ev-admin-auth-failures", "ev-admin-lockout"},
    "internal_policy_deny_expected": {"ev-policy-deny-profile", "ev-traffic-baseline"},
    "benign_session_clash": {"ev-event-log-scan"},
    "dhcp_service_healthy": {"ev-dhcp-health"},
    "security_posture_current": {"ev-security-posture"},
    "device_service_port_probe_contained": {"ev-device-port-probe"},
    "firewall_resource_healthy": {"ev-firewall-resource"},
}


def build_diagnosis(case, evidence: list[dict], context) -> RCADiagnosis:
    context_evidence, context_missing = _evidence_selected_by_context(case, evidence, context)
    root_cause_key, cited = _infer_from_evidence(context_evidence)
    return RCADiagnosis(
        case_id=case.id,
        root_cause_key=root_cause_key,
        root_cause=ROOT_CAUSES.get(root_cause_key, "Unknown root cause"),
        confidence=0.92 if root_cause_key != "unknown" else 0.2,
        evidence=[
            DiagnosisEvidence(
                evidence_id=item["evidence_id"],
                source=item["source"],
                summary=item["summary"],
            )
            for item in cited
        ],
        missing_evidence=context_missing,
        recommended_actions=_actions(root_cause_key),
        readonly=True,
    )


class LLMReasoner:
    def __init__(self, client):
        self.client = client

    def __call__(self, case, evidence: list[dict], context) -> RCADiagnosis:
        allowed = sorted(ROOT_CAUSES)
        context_evidence, context_missing = _evidence_selected_by_context(case, evidence, context)
        evidence_ids = [item["evidence_id"] for item in context_evidence]
        instruction = (
            "You are a network root-cause analyst. Pick exactly one root_cause_key from "
            "allowed_root_cause_keys. Reason from the supplied compiled_context and cite only "
            "its included evidence ids. Dropped provenance is metadata about omitted context, "
            "not evidence.\n"
            "Return ONLY a JSON object with EXACTLY these fields:\n"
            '{"root_cause_key": <one of allowed_root_cause_keys>, '
            '"confidence": <float 0..1>, '
            '"evidence_ids": [<subset of the provided evidence_id strings>], '
            '"recommended_actions": [<short readonly strings>], '
            '"readonly": true}\n'
            "Do not invent evidence ids. root_cause_key MUST be one of the allowed keys."
        )
        payload = self.client.complete_json(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "instruction": instruction,
                            "case": case.model_dump(),
                            "compiled_context": _context_payload(context),
                            "available_evidence_ids": evidence_ids,
                            "allowed_root_cause_keys": allowed,
                            "readonly_only": True,
                        },
                        sort_keys=True,
                    ),
                }
            ],
            schema_name="network_rca_diagnosis",
        )
        # Lenient parse: accept root_cause_key or root_cause; evidence_ids or evidence[].
        by_id = {item["evidence_id"]: item for item in context_evidence}
        root_cause_key = payload.get("root_cause_key") or payload.get("root_cause") or "unknown"
        if root_cause_key not in ROOT_CAUSES:
            root_cause_key = "unknown"
        raw_ids = payload.get("evidence_ids")
        if not raw_ids:
            raw_ids = [item.get("evidence_id") for item in payload.get("evidence", []) if isinstance(item, dict)]
        cited = [by_id[eid] for eid in (raw_ids or []) if eid in by_id]
        reported_missing = list(payload.get("missing_evidence", []))
        missing_evidence = list(dict.fromkeys([*context_missing, *reported_missing]))
        return RCADiagnosis(
            case_id=case.id,
            root_cause_key=root_cause_key,
            root_cause=payload.get("root_cause") or ROOT_CAUSES.get(root_cause_key, "Unknown root cause"),
            confidence=float(payload.get("confidence", 0.0)),
            evidence=[
                DiagnosisEvidence(
                    evidence_id=item["evidence_id"],
                    source=item["source"],
                    summary=item["summary"],
                )
                for item in cited
            ],
            missing_evidence=missing_evidence,
            recommended_actions=list(payload.get("recommended_actions", [])),
            readonly=bool(payload.get("readonly", True)),
        )


def _evidence_selected_by_context(case, evidence: list[dict], context) -> tuple[list[dict], list[str]]:
    """Validate the compiler contract and expose only evidence kept in context."""

    if context is None:
        raise ValueError("network RCA reasoning requires a compiled ContextPacket")
    if getattr(context, "case_id", None) != case.id:
        raise ValueError(
            f"context case_id {getattr(context, 'case_id', None)!r} does not match case {case.id!r}"
        )

    included_ids = list(getattr(context, "included_evidence_ids", []))
    if len(included_ids) != len(set(included_ids)):
        raise ValueError("compiled context contains duplicate included evidence ids")
    current_ids = [item["evidence_id"] for item in evidence]
    if len(current_ids) != len(set(current_ids)):
        raise ValueError("current evidence contains duplicate evidence ids")
    by_id = {item["evidence_id"]: item for item in evidence}
    unknown = [evidence_id for evidence_id in included_ids if evidence_id not in by_id]
    if unknown:
        raise ValueError(f"compiled context references unavailable current evidence ids: {unknown}")

    provenance_ids = [
        item.item_id
        for section in getattr(context, "sections", [])
        for item in section.kept
        if item.kind == "evidence" and item.item_id
    ]
    if provenance_ids != included_ids:
        raise ValueError(
            "compiled context evidence provenance does not match included_evidence_ids: "
            f"{provenance_ids!r} != {included_ids!r}"
        )

    missing = list(getattr(context, "missing_evidence", []))
    overlap = sorted(set(included_ids) & set(missing))
    if overlap:
        raise ValueError(f"compiled context marks evidence as both included and missing: {overlap}")
    return [by_id[evidence_id] for evidence_id in included_ids], missing


def _context_payload(context) -> dict:
    """Serialize useful context once, without duplicating raw evidence objects."""

    return {
        "summary": context.summary,
        "included_memory_ids": list(context.included_memory_ids),
        "included_evidence_ids": list(context.included_evidence_ids),
        "missing_evidence": list(context.missing_evidence),
        "estimated_tokens": context.estimated_tokens_after,
        "compiler_mode": context.compiler_mode,
        "sections": [
            {
                "name": section.name,
                "token_budget": section.token_budget,
                "estimated_tokens_before": section.estimated_tokens_before,
                "estimated_tokens_after": section.estimated_tokens_after,
                "budget_overflow_tokens": section.budget_overflow_tokens,
                "kept": [
                    {
                        "kind": item.kind,
                        "item_id": item.item_id,
                        "required": item.required,
                        "truncated": item.truncated,
                    }
                    for item in section.kept
                ],
                "dropped": [
                    {
                        "kind": item.kind,
                        "item_id": item.item_id,
                        "required": item.required,
                        "truncated": item.truncated,
                        "reason": item.reason,
                    }
                    for item in section.dropped
                ],
            }
            for section in context.sections
        ],
    }


def _infer_from_evidence(evidence: list[dict]) -> tuple[str, list[dict]]:
    by_id = {item["evidence_id"]: item for item in evidence}
    data = {item["evidence_id"]: item.get("data", {}) for item in evidence}

    if data.get("ev-eno1-oper-down", {}).get("carrier") is False and data.get("ev-eno1-no-phy", {}).get("link_detected") is False:
        return "carrier_down", [by_id["ev-eno1-oper-down"], by_id["ev-eno1-no-phy"]]

    eno2_drop = data.get("ev-eno2-dropped", {}).get("rx_dropped", 0)
    no_hw_errors = data.get("ev-eno2-no-hw-errors", {})
    if eno2_drop > 0 and all(no_hw_errors.get(key) == 0 for key in ("rx_errors", "crc_errors", "frame_errors", "fifo_errors")):
        return "benign_rx_dropped", [by_id["ev-eno2-dropped"], by_id["ev-eno2-no-hw-errors"]]

    if "ev-fg-connected-routes" in by_id and data.get("ev-fg-policy-deny", {}).get("action") == "deny":
        return "fg_policy_missing", [by_id["ev-fg-connected-routes"], by_id["ev-fg-policy-deny"]]

    license_data = data.get("ev-fortiguard-expired", {})
    forwarding_data = data.get("ev-forwarding-ok", {})
    expired = all(license_data.get(key) == "expired" for key in ("av", "ips", "webfilter"))
    forwarding_ok = forwarding_data.get("default_route") is True and forwarding_data.get("sessions_forwarding") is True
    if expired and forwarding_ok:
        return "security_subscription_expired_forwarding_ok", [by_id["ev-fortiguard-expired"], by_id["ev-forwarding-ok"]]

    if "ev-vip-map-8443" in by_id and "ev-vip-policy-service-mismatch" in by_id:
        return "vip_policy_mismatch", [by_id["ev-vip-map-8443"], by_id["ev-vip-policy-service-mismatch"]]

    # Real FortiGate-syslog evidence (held-out network RCA).
    failed = data.get("ev-admin-auth-failures", {})
    lockout = data.get("ev-admin-lockout", {})
    if failed.get("failed_login_count", 0) >= 1000 and lockout.get("lockout_events", 0) >= 1:
        return "admin_bruteforce_lockout", [by_id["ev-admin-auth-failures"], by_id["ev-admin-lockout"]]

    deny = data.get("ev-policy-deny-profile", {})
    if (
        deny.get("deny_count", 0) >= 10000
        and deny.get("internal_src_ratio", 0) >= 0.5
        and "ev-traffic-baseline" in by_id
    ):
        return "internal_policy_deny_expected", [by_id["ev-policy-deny-profile"], by_id["ev-traffic-baseline"]]

    if data.get("ev-dhcp-health", {}).get("dhcp_ack", 0) > 0:
        return "dhcp_service_healthy", [by_id["ev-dhcp-health"]]

    if data.get("ev-security-posture", {}).get("updates", 0) > 0:
        return "security_posture_current", [by_id["ev-security-posture"]]

    if data.get("ev-device-port-probe", {}).get("device_port_deny", 0) > 0:
        return "device_service_port_probe_contained", [by_id["ev-device-port-probe"]]

    resource = data.get("ev-firewall-resource", {})
    if "ev-firewall-resource" in by_id and resource.get("cpu", 100) < 60 and resource.get("mem", 100) < 80:
        return "firewall_resource_healthy", [by_id["ev-firewall-resource"]]

    event_scan = data.get("ev-event-log-scan", {})
    if event_scan.get("session_clash", 0) > 0 and not failed and not deny:
        return "benign_session_clash", [by_id["ev-event-log-scan"]]

    return "unknown", []


def _actions(root_cause_key: str) -> list[str]:
    return {
        "carrier_down": ["Check cabling and peer switch port state before replacing the NIC."],
        "benign_rx_dropped": ["Monitor counter deltas over time and correlate with broadcast or multicast traffic."],
        "fg_policy_missing": ["Review FortiGate policy and address objects with human approval before changes."],
        "security_subscription_expired_forwarding_ok": ["Renew security subscriptions if inspection coverage is required."],
        "vip_policy_mismatch": ["Review VIP and policy service mapping with human approval before changes."],
        "admin_bruteforce_lockout": [
            "Restrict admin access to trusted management hosts and review trusthost / GeoIP policy with human approval; do not treat as a device fault.",
        ],
        "internal_policy_deny_expected": [
            "Confirm the denied internal flows are unwanted (NetBIOS and similar) and tune host/app behaviour; the firewall deny is working as intended.",
        ],
        "benign_session_clash": ["No action required; informational events only."],
        "dhcp_service_healthy": ["Confirm scope utilization stays within range during peak hours; service is healthy."],
        "security_posture_current": ["Keep the FortiGuard update schedule; posture is current."],
        "device_service_port_probe_contained": [
            "Confirm camera/DVR service ports stay closed at the WAN edge and keep the containing policy; exposure is controlled.",
        ],
        "firewall_resource_healthy": ["Continue monitoring; resource headroom is ample."],
    }.get(root_cause_key, ["Collect more readonly evidence."])
