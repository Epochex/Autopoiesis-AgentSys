from __future__ import annotations

from domains.network_rca.schema import DiagnosisEvidence, RCADiagnosis


RISK_DESCRIPTIONS = {
    "critical_cve_exposed": "A public service has a critical CVE match and should be treated as the highest remediation priority.",
    "internet_exposed_admin": "An administrative service is reachable on the target surface.",
    "public_database_exposure": "A database service is exposed on the target surface.",
    "weak_tls_exposed": "A public TLS service uses weak or expired transport security.",
    "informational_exposure": "Only low-risk exposed services were observed in the mock recon evidence.",
    "no_exposure_observed": "No open service exposure was observed in the collected mock recon evidence.",
    "unknown": "Insufficient mock recon evidence was collected to rank exposure.",
}


def build_situational_picture(evidence: list[dict]) -> dict:
    assets: dict[str, dict] = {}
    exposures: list[dict] = []
    cve_severity = 0.0
    weak_tls = False
    admin_exposed = False
    database_exposed = False

    for item in evidence:
        host = item.get("host") or item.get("data", {}).get("host")
        if not host:
            continue
        asset = assets.setdefault(host, {"open_services": [], "evidence_ids": []})
        asset["evidence_ids"].append(item["evidence_id"])
        data = item.get("data", {})
        port = item.get("port") or data.get("port")
        service = item.get("service") or data.get("service")
        if port and service:
            service_key = f"{host}:{port}/{service}"
            if service_key not in asset["open_services"]:
                asset["open_services"].append(service_key)
            if service_key not in [exposure["service"] for exposure in exposures]:
                exposures.append({"service": service_key, "evidence_ids": [item["evidence_id"]]})
            else:
                for exposure in exposures:
                    if exposure["service"] == service_key:
                        exposure["evidence_ids"].append(item["evidence_id"])
                        break
        service_l = str(service or "").lower()
        if service_l in {"ssh", "rdp", "admin-http", "admin-https"}:
            admin_exposed = True
        if service_l in {"postgres", "mysql", "mongodb", "redis"}:
            database_exposed = True
        tls_status = data.get("tls_status") or item.get("tls_status")
        if tls_status in {"expired", "weak_cipher", "self_signed_weak"}:
            weak_tls = True
        severity = float(data.get("severity", item.get("severity", 0.0)) or 0.0)
        cve_severity = max(cve_severity, severity)

    risk_score = 0
    if cve_severity >= 9.0:
        top_risk = "critical_cve_exposed"
        risk_score = 95
    elif admin_exposed:
        top_risk = "internet_exposed_admin"
        risk_score = 82
    elif database_exposed:
        top_risk = "public_database_exposure"
        risk_score = 78
    elif weak_tls:
        top_risk = "weak_tls_exposed"
        risk_score = 58
    elif exposures:
        top_risk = "informational_exposure"
        risk_score = 25
    elif evidence:
        top_risk = "no_exposure_observed"
    else:
        top_risk = "unknown"

    return {
        "assets": assets,
        "exposures": exposures,
        "risk_score": risk_score,
        "top_risk": top_risk,
    }


def build_recon_diagnosis(case, evidence: list[dict], context) -> RCADiagnosis:
    picture = build_situational_picture(evidence)
    top_risk = picture["top_risk"]
    cited = _supporting_evidence(top_risk, evidence)
    if not cited and evidence:
        cited = evidence[:1]
    return RCADiagnosis(
        case_id=case.id,
        root_cause_key=top_risk,
        root_cause=RISK_DESCRIPTIONS.get(top_risk, RISK_DESCRIPTIONS["unknown"]),
        confidence=0.9 if top_risk not in {"unknown", "no_exposure_observed"} else 0.45,
        evidence=[
            DiagnosisEvidence(
                evidence_id=item["evidence_id"],
                source=item["source"],
                summary=item["summary"],
            )
            for item in cited
        ],
        missing_evidence=[],
        recommended_actions=_actions(top_risk),
        readonly=True,
    )


def _supporting_evidence(top_risk: str, evidence: list[dict]) -> list[dict]:
    if top_risk == "critical_cve_exposed":
        return [item for item in evidence if float(item.get("data", {}).get("severity", item.get("severity", 0.0)) or 0.0) >= 9.0]
    if top_risk == "internet_exposed_admin":
        return [
            item
            for item in evidence
            if str(item.get("service") or item.get("data", {}).get("service") or "").lower()
            in {"ssh", "rdp", "admin-http", "admin-https"}
        ]
    if top_risk == "public_database_exposure":
        return [
            item
            for item in evidence
            if str(item.get("service") or item.get("data", {}).get("service") or "").lower()
            in {"postgres", "mysql", "mongodb", "redis"}
        ]
    if top_risk == "weak_tls_exposed":
        return [
            item
            for item in evidence
            if (item.get("data", {}).get("tls_status") or item.get("tls_status"))
            in {"expired", "weak_cipher", "self_signed_weak"}
        ]
    if top_risk in {"informational_exposure", "no_exposure_observed"}:
        return evidence[:2]
    return []


def _actions(top_risk: str) -> list[str]:
    return {
        "critical_cve_exposed": ["Prioritize patching or isolation through the normal change-approval process."],
        "internet_exposed_admin": ["Restrict administrative access to approved management networks after human review."],
        "public_database_exposure": ["Validate business need and place the database behind approved access controls."],
        "weak_tls_exposed": ["Plan certificate and TLS policy remediation through approved maintenance."],
        "informational_exposure": ["Continue readonly monitoring and confirm ownership of the exposed service."],
        "no_exposure_observed": ["No remediation from current readonly evidence; keep scheduled recon coverage."],
    }.get(top_risk, ["Collect more readonly mock evidence."])
