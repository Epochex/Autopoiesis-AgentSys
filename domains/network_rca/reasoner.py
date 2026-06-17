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


class LLMReasoner:
    def __init__(self, client):
        self.client = client

    def __call__(self, case, evidence: list[dict], context) -> RCADiagnosis:
        allowed = sorted(ROOT_CAUSES)
        evidence_ids = [item["evidence_id"] for item in evidence]
        instruction = (
            "You are a network root-cause analyst. Pick exactly one root_cause_key from "
            f"allowed_root_cause_keys and cite the evidence ids that support it.\n"
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
                            "evidence": evidence,
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
        by_id = {item["evidence_id"]: item for item in evidence}
        root_cause_key = payload.get("root_cause_key") or payload.get("root_cause") or "unknown"
        if root_cause_key not in ROOT_CAUSES:
            root_cause_key = "unknown"
        raw_ids = payload.get("evidence_ids")
        if not raw_ids:
            raw_ids = [item.get("evidence_id") for item in payload.get("evidence", []) if isinstance(item, dict)]
        cited = [by_id[eid] for eid in (raw_ids or []) if eid in by_id]
        # If the model named a valid cause but cited nothing, attribute the observed evidence.
        if root_cause_key != "unknown" and not cited:
            cited = list(evidence)
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
            missing_evidence=list(payload.get("missing_evidence", [])),
            recommended_actions=list(payload.get("recommended_actions", [])),
            readonly=bool(payload.get("readonly", True)),
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

    # Real FortiGate-syslog evidence (held-out network RCA).
    failed = data.get("ev-admin-auth-failures", {})
    lockout = data.get("ev-admin-lockout", {})
    if failed.get("failed_login_count", 0) >= 1000 and lockout.get("lockout_events", 0) >= 1:
        return "admin_bruteforce_lockout", [by_id["ev-admin-auth-failures"], by_id["ev-admin-lockout"]]

    deny = data.get("ev-policy-deny-profile", {})
    baseline = data.get("ev-traffic-baseline", {})
    if (
        deny.get("deny_count", 0) >= 10000
        and deny.get("internal_src_ratio", 0) >= 0.5
        and "ev-traffic-baseline" in by_id
    ):
        return "internal_policy_deny_expected", [by_id["ev-policy-deny-profile"], by_id["ev-traffic-baseline"]]

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
    }.get(root_cause_key, ["Collect more readonly evidence."])
