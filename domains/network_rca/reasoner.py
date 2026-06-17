from __future__ import annotations

from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


ROOT_CAUSES = {
    "carrier_down": "eno1 has no carrier because the cable is unplugged or the peer switch port is down; this is not a NIC hardware fault.",
    "benign_rx_dropped": "eno2 has high cumulative RX dropped counters with zero hardware errors, consistent with broadcast or multicast pressure or historical accumulation; keep observing instead of replacing hardware.",
    "fg_policy_missing": "Showroom to office reachability fails at FortiGate layer-3 policy or address-object matching, not at layer 2.",
    "security_subscription_expired_forwarding_ok": "Expired FortiGuard AV, IPS, and WebFilter subscriptions reduce security inspection coverage but do not block basic forwarding.",
    "vip_policy_mismatch": "The WAN VIP port mapping is inconsistent with the matching firewall policy or service object, so the specific published service is not reachable.",
}


def build_diagnosis(case, evidence: list[dict], context) -> RCADiagnosis:
    root_cause_key, cited = _infer_from_evidence(evidence)
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
        missing_evidence=[],
        recommended_actions=_actions(root_cause_key),
        readonly=True,
    )


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

    return "unknown", []


def _actions(root_cause_key: str) -> list[str]:
    return {
        "carrier_down": ["Check cabling and peer switch port state before replacing the NIC."],
        "benign_rx_dropped": ["Monitor counter deltas over time and correlate with broadcast or multicast traffic."],
        "fg_policy_missing": ["Review FortiGate policy and address objects with human approval before changes."],
        "security_subscription_expired_forwarding_ok": ["Renew security subscriptions if inspection coverage is required."],
        "vip_policy_mismatch": ["Review VIP and policy service mapping with human approval before changes."],
    }.get(root_cause_key, ["Collect more readonly evidence."])
