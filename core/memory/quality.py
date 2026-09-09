"""Quality contract for learned operational memories.

A memory is useful when it preserves evidence-bearing claims that can change a
future investigation.  Fixed prose templates are intentionally avoided: a
record carries only the claims observed in the source trace, and every decisive
claim names its evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.memory.store import MemoryRecord


@dataclass(frozen=True, slots=True)
class MemoryQuality:
    score: float
    retrieval_ready: bool
    issues: tuple[str, ...]


_REQUIRED_CLAIMS = {
    "episodic": frozenset({"symptom", "root_cause", "outcome"}),
    "semantic": frozenset({"root_cause", "applicability"}),
    "procedural": frozenset({"trigger", "probe", "expected_signal"}),
    "asset_profile": frozenset({"observation"}),
}
_DECISIVE_KINDS = frozenset({"observation", "root_cause", "outcome", "expected_signal"})


def assess_memory(record: "MemoryRecord") -> MemoryQuality:
    """Evaluate evidence coverage and substantive depth for one learned record.

    Legacy hand-authored records without atomic claims remain readable.  New
    consolidation code calls this function explicitly and stores the result, so
    shallow generated records can be retained for audit without entering recall.
    """
    issues: list[str] = []
    claims = list(record.claims)
    kinds = {claim.kind for claim in claims if claim.statement.strip()}
    required = _REQUIRED_CLAIMS[record.tier]
    missing = sorted(required - kinds)
    issues.extend(f"missing_claim:{kind}" for kind in missing)

    substantive = [claim for claim in claims if len(claim.statement.strip()) >= 8]
    if len(substantive) < len(required):
        issues.append("insufficient_substantive_claims")

    evidence_linked = [
        claim for claim in claims
        if claim.kind in _DECISIVE_KINDS and claim.evidence_ids
    ]
    if record.tier in {"episodic", "semantic", "procedural"} and not evidence_linked:
        issues.append("no_evidence_linked_claim")

    if record.tier in {"episodic", "procedural"} and not record.source_trace_ids:
        issues.append("missing_source_trace")

    checks = 4
    passed = checks - sum((bool(missing), len(substantive) < len(required), not evidence_linked,
                           record.tier in {"episodic", "procedural"} and not record.source_trace_ids))
    score = max(0.0, min(1.0, passed / checks))
    return MemoryQuality(
        score=score,
        retrieval_ready=not issues,
        issues=tuple(dict.fromkeys(issues)),
    )


def apply_memory_quality(record: "MemoryRecord") -> MemoryQuality:
    """Persist the quality decision on a newly synthesized memory record."""
    result = assess_memory(record)
    record.quality_score = result.score
    record.retrieval_ready = result.retrieval_ready
    record.quality_issues = list(result.issues)
    return result


__all__ = ["MemoryQuality", "apply_memory_quality", "assess_memory"]
