"""Evidence construction and deterministic gates for live LLM graph claims."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def evidence_id(kind: str, fact: dict[str, Any]) -> str:
    """Return a stable identifier for an input fact, independent of list order."""
    canonical = json.dumps(
        {"kind": kind, "fact": fact}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"ev-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def evidence_fact(
    kind: str,
    fact: dict[str, Any],
    *,
    subjects: list[str] | tuple[str, ...] = (),
    pair: tuple[str, str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "evidence_id": evidence_id(kind, fact),
        "kind": kind,
        "subjects": sorted(set(subjects)),
        "fact": fact,
    }
    if pair is not None:
        item["pair"] = sorted(pair)
    return item


def _claim_evidence_ids(claim: dict[str, Any]) -> list[str]:
    refs = claim.get("evidence_ids")
    if not isinstance(refs, list):
        return []
    return list(dict.fromkeys(str(ref) for ref in refs if isinstance(ref, str) and ref))


def verify_pair_claims(
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    source_field: str,
    target_field: str,
    fixed_source: str | None = None,
    field: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept relationship claims only when cited evidence supports that exact pair."""
    registry = {item["evidence_id"]: item for item in evidence}
    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        src = fixed_source if fixed_source is not None else claim.get(source_field)
        dst = claim.get(target_field)
        refs = _claim_evidence_ids(claim)
        reason = ""
        if not src or not dst or src == dst:
            reason = "invalid relationship endpoints"
        elif not refs:
            reason = "relationship has no evidence_ids"
        elif any(ref not in registry for ref in refs):
            reason = "relationship cites an unknown evidence_id"
        else:
            pair = sorted((str(src), str(dst)))
            if not any(item.get("pair") == pair for item in (registry[ref] for ref in refs)):
                reason = "cited evidence does not support this relationship"
        if reason:
            rejected.append({**claim, "verificationError": reason})
            errors.append({"field": field, "index": index, "reason": reason})
            continue
        verified.append({**claim, "evidenceIds": refs})
    return verified, rejected, errors


def verify_graph_claims(
    patterns: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    valid_ips: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Verify graph claims against cited atomic facts and observed graph edges."""
    registry = {item["evidence_id"]: item for item in evidence}
    verified_patterns: list[dict[str, Any]] = []
    rejected_patterns: list[dict[str, Any]] = []
    verified_corridors: list[dict[str, Any]] = []
    rejected_corridors: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    compatible_kinds = {
        "shadow-fleet": {"graph_community"},
        "misconfig": {"graph_anomaly"},
        "duplicate-ip": {"graph_anomaly"},
        "lateral-corridor": {"graph_edge"},
        "blind-spot": {"graph_anomaly", "device_profile"},
        "exposure": {"graph_edge", "device_profile"},
    }

    for index, pattern in enumerate(patterns):
        raw_members = pattern.get("members") or []
        members = [m for m in raw_members if m in valid_ips]
        refs = _claim_evidence_ids(pattern)
        reason = ""
        if not isinstance(raw_members, list) or not members:
            reason = "pattern has no valid members"
        elif len(members) != len(raw_members):
            reason = "pattern contains an unknown member"
        elif not refs:
            reason = "pattern has no evidence_ids"
        elif any(ref not in registry for ref in refs):
            reason = "pattern cites an unknown evidence_id"
        else:
            cited = [registry[ref] for ref in refs]
            allowed = compatible_kinds.get(pattern.get("kind"))
            supported = {subject for item in cited for subject in item.get("subjects", [])}
            if not set(members).issubset(supported):
                reason = "cited evidence does not cover every pattern member"
            elif allowed is not None and not any(item.get("kind") in allowed for item in cited):
                reason = "cited evidence type does not support this pattern kind"
        if reason:
            rejected_patterns.append({**pattern, "members": members, "verificationError": reason})
            errors.append({"field": "patterns", "index": index, "reason": reason})
        else:
            verified_patterns.append({**pattern, "members": members, "evidenceIds": refs})

    for index, corridor in enumerate(corridors):
        src, dst = corridor.get("src"), corridor.get("dst")
        path = corridor.get("path") or [src, dst]
        refs = _claim_evidence_ids(corridor)
        reason = ""
        if (
            not isinstance(path, list)
            or len(path) < 2
            or path[0] != src
            or path[-1] != dst
            or any(ip not in valid_ips for ip in path)
            or len(set(path)) != len(path)
        ):
            reason = "corridor path is invalid"
        elif not refs:
            reason = "corridor has no evidence_ids"
        elif any(ref not in registry for ref in refs):
            reason = "corridor cites an unknown evidence_id"
        else:
            cited_edges = {
                tuple(item["pair"])
                for item in (registry[ref] for ref in refs)
                if item.get("kind") == "graph_edge" and item.get("pair")
            }
            required_edges = {tuple(sorted((a, b))) for a, b in zip(path, path[1:])}
            if not required_edges.issubset(cited_edges):
                reason = "corridor is not backed by every edge in its path"
        if reason:
            rejected_corridors.append({**corridor, "path": path, "verificationError": reason})
            errors.append({"field": "corridors", "index": index, "reason": reason})
        else:
            verified_corridors.append({**corridor, "path": path, "evidenceIds": refs})

    return (
        verified_patterns,
        verified_corridors,
        {"patterns": rejected_patterns, "corridors": rejected_corridors},
        errors,
    )


def _ports(item: dict[str, Any]) -> set[Any]:
    return set(item.get("top_ports") or item.get("topPorts") or item.get("ports") or [])


def relationship_evidence(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any] | None:
    """Derive an auditable pair fact from two host profiles; return None for mere co-presence."""
    src_ip, dst_ip = source.get("ip"), target.get("ip")
    if not src_ip or not dst_ip or src_ip == dst_ip:
        return None
    shared_ports = sorted(_ports(source) & _ports(target), key=str)
    same_risk = source.get("threat") == target.get("threat") and source.get("threat") in {"high", "watch"}
    same_role = source.get("role") == target.get("role") and bool(source.get("role"))
    if not shared_ports and not same_risk and not same_role:
        return None
    fact = {
        "source": src_ip,
        "target": dst_ip,
        "shared_ports": shared_ports,
        "same_elevated_risk": same_risk,
        "same_role": same_role,
    }
    return evidence_fact("host_relationship", fact, subjects=[src_ip, dst_ip], pair=(src_ip, dst_ip))
