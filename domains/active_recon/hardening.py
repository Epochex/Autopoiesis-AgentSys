from __future__ import annotations

from pydantic import BaseModel


RISK_ORDER = [
    "critical_cve_exposed",
    "internet_exposed_admin",
    "public_database_exposure",
    "weak_tls_exposed",
    "informational_exposure",
]

HARDENING_ACTIONS = {
    "critical_cve_exposed": "Patch the affected service or isolate it from exposed networks through the approved change process.",
    "internet_exposed_admin": "Restrict administrative access to approved management networks.",
    "public_database_exposure": "Place the database service behind approved access control.",
    "weak_tls_exposed": "Remediate TLS policy and certificate configuration.",
    "informational_exposure": "Continue readonly monitoring and ownership validation.",
}

HARDENING_RATIONALES = {
    "critical_cve_exposed": "Readonly evidence shows an exposed service with critical CVE severity, so remediation should be prioritized before lower-risk exposure cleanup.",
    "internet_exposed_admin": "Readonly evidence shows a reachable administrative service, which should be limited to trusted management paths.",
    "public_database_exposure": "Readonly evidence shows a database service on the observed target surface, so access should be explicitly controlled.",
    "weak_tls_exposed": "Readonly evidence shows weak or expired TLS posture on an exposed service, increasing downgrade and trust risks.",
    "informational_exposure": "Readonly evidence shows exposed services without a higher-risk finding; continued observation is non-mutating.",
}

APPROVAL_REQUIRED = {
    "critical_cve_exposed",
    "internet_exposed_admin",
    "public_database_exposure",
    "weak_tls_exposed",
}


class HardeningReport(BaseModel):
    recommendations: list[dict]
    generated_from_case_id: str | None = None


def recommend_hardening(picture: dict, evidence: list[dict]) -> list[dict]:
    observed_ids = {item["evidence_id"] for item in evidence if "evidence_id" in item}
    candidate_risks = _candidate_risks(picture, evidence)

    recommendations: list[dict] = []
    for risk in sorted(candidate_risks, key=RISK_ORDER.index):
        evidence_ids = _supporting_evidence_ids(risk, picture, evidence, observed_ids)
        if not evidence_ids:
            continue
        recommendations.append(
            {
                "risk": risk,
                "action": HARDENING_ACTIONS[risk],
                "priority": len(recommendations) + 1,
                "rationale": HARDENING_RATIONALES[risk],
                "evidence_ids": evidence_ids,
                "requires_approval": risk in APPROVAL_REQUIRED,
            }
        )

    return recommendations


def _candidate_risks(picture: dict, evidence: list[dict]) -> set[str]:
    risks: set[str] = set()
    top_risk = picture.get("top_risk")
    if top_risk in HARDENING_ACTIONS:
        risks.add(top_risk)

    for item in evidence:
        service = str(item.get("service") or item.get("data", {}).get("service") or "").lower()
        tls_status = item.get("data", {}).get("tls_status") or item.get("tls_status")
        severity = float(item.get("data", {}).get("severity", item.get("severity", 0.0)) or 0.0)

        if severity >= 9.0:
            risks.add("critical_cve_exposed")
        if service in {"ssh", "rdp", "admin-http", "admin-https"}:
            risks.add("internet_exposed_admin")
        if service in {"postgres", "mysql", "mongodb", "redis"}:
            risks.add("public_database_exposure")
        if tls_status in {"expired", "weak_cipher", "self_signed_weak"}:
            risks.add("weak_tls_exposed")

    if not risks and picture.get("exposures"):
        risks.add("informational_exposure")
    if risks == {"informational_exposure"}:
        return risks
    return risks - {"informational_exposure"}


def _supporting_evidence_ids(
    risk: str,
    picture: dict,
    evidence: list[dict],
    observed_ids: set[str],
) -> list[str]:
    if risk == "critical_cve_exposed":
        ids = [
            item["evidence_id"]
            for item in evidence
            if float(item.get("data", {}).get("severity", item.get("severity", 0.0)) or 0.0) >= 9.0
        ]
    elif risk == "internet_exposed_admin":
        ids = [
            item["evidence_id"]
            for item in evidence
            if str(item.get("service") or item.get("data", {}).get("service") or "").lower()
            in {"ssh", "rdp", "admin-http", "admin-https"}
        ]
    elif risk == "public_database_exposure":
        ids = [
            item["evidence_id"]
            for item in evidence
            if str(item.get("service") or item.get("data", {}).get("service") or "").lower()
            in {"postgres", "mysql", "mongodb", "redis"}
        ]
    elif risk == "weak_tls_exposed":
        ids = [
            item["evidence_id"]
            for item in evidence
            if (item.get("data", {}).get("tls_status") or item.get("tls_status"))
            in {"expired", "weak_cipher", "self_signed_weak"}
        ]
    elif risk == "informational_exposure":
        ids = [
            evidence_id
            for exposure in picture.get("exposures", [])
            for evidence_id in exposure.get("evidence_ids", [])
        ]
    else:
        ids = []

    return list(dict.fromkeys(evidence_id for evidence_id in ids if evidence_id in observed_ids))
