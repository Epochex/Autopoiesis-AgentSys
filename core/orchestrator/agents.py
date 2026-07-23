from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from core.skills.registry import SkillRegistry
from core.verifier.verifier import VerificationReport


TraceRecorder = Callable[[str, str, dict], None]


def _noop_record(case_id: str, kind: str, payload: dict) -> None:
    return None


@dataclass(frozen=True)
class RoleDefinition:
    """One read-only diagnostic perspective and the tokens that route skills to it."""

    name: str
    tokens: frozenset[str]


DEFAULT_DIAGNOSTIC_ROLES: tuple[RoleDefinition, ...] = (
    RoleDefinition(
        "temporal",
        frozenset({"time", "timeline", "temporal", "event", "log", "history", "sequence", "trend"}),
    ),
    RoleDefinition(
        "topology",
        frozenset({
            "topology", "route", "routing", "link", "carrier", "interface", "peer",
            "segment", "switch", "vlan", "lacp", "network",
        }),
    ),
    RoleDefinition(
        "configuration",
        frozenset({
            "config", "configuration", "policy", "address", "object", "vip", "nat",
            "dhcp", "wan", "subscription", "fortigate", "forwarding",
        }),
    ),
    RoleDefinition(
        "security",
        frozenset({
            "security", "cve", "vulnerability", "risk", "tls", "certificate", "cipher",
            "scan", "exposed", "exploit", "credential", "password", "ips", "av", "webfilter",
        }),
    ),
)


@dataclass(frozen=True)
class RoleAssignment:
    """A specialist role and its ordered, read-only skill probes."""

    role: str
    skill_names: tuple[str, ...]
    score: float = 0.0


@dataclass
class RoleFinding:
    """Auditable output of one specialist; it contains evidence, never actions."""

    role: str
    skill_names: list[str]
    evidence: list[dict] = field(default_factory=list)
    cost: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    worker_thread: int = 0
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.finished_at - self.started_at) * 1_000.0)


class PlannerAgent:
    """Propose the next read-only skills for an unresolved case.

    Scores unexecuted, unfrozen read-only skills by overlap with the diagnosis's
    missing evidence (weight 3), the query terms (weight 1), and the case's
    preferred skills (+2); falls back to the cheapest unexecuted skills when
    nothing matches. Emits a `planner_proposed` trace event per proposal.
    """

    MISSING_EVIDENCE_WEIGHT = 3.0
    QUERY_TERM_WEIGHT = 1.0
    PREFERRED_BONUS = 2.0

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        batch_size: int = 2,
        record: TraceRecorder | None = None,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self.registry = registry
        self.batch_size = batch_size
        self._record = record or _noop_record

    def propose(self, case: Any, diagnosis: Any, executed: set[str]) -> list[str]:
        """Return up to `batch_size` skill names to probe next (may be empty).

        `case` needs `id`; `query_terms`/`relevant_skills` and the diagnosis's
        `missing_evidence` are read defensively when present.
        """
        missing_tokens = _tokens(getattr(diagnosis, "missing_evidence", []))
        query_tokens = _tokens(getattr(case, "query_terms", []))
        preferred = set(getattr(case, "relevant_skills", []))

        scored: list[tuple[float, float, str]] = []
        fallback: list[tuple[float, str]] = []
        for skill in self.registry.all():
            spec = skill.spec
            if spec.name in executed or spec.frozen or spec.risk != "read_only":
                continue
            skill_tokens = _tokens([spec.name, spec.description, *spec.tags])
            score = self.MISSING_EVIDENCE_WEIGHT * len(missing_tokens & skill_tokens)
            score += self.QUERY_TERM_WEIGHT * len(query_tokens & skill_tokens)
            if spec.name in preferred:
                score += self.PREFERRED_BONUS
            if score > 0:
                scored.append((score, spec.cost, spec.name))
            else:
                fallback.append((spec.cost, spec.name))

        if scored:
            ordered = [name for _, _, name in sorted(scored, key=lambda item: (-item[0], item[1], item[2]))]
        else:
            ordered = [name for _, name in sorted(fallback)]
        proposed = ordered[: self.batch_size]
        self._record(
            case.id,
            "planner_proposed",
            {
                "skills": proposed,
                "missing_evidence": list(getattr(diagnosis, "missing_evidence", [])),
                "executed": sorted(executed),
            },
        )
        return proposed

    def plan_parallel(
        self,
        case: Any,
        diagnosis: Any,
        executed: set[str],
        *,
        max_roles: int,
        roles: tuple[RoleDefinition, ...] = DEFAULT_DIAGNOSTIC_ROLES,
    ) -> list[RoleAssignment]:
        """Build a bounded specialist plan over unique, read-only skills.

        The first pass gives each useful perspective one probe before a second
        probe is assigned to any role.  This avoids disguising four concurrent
        copies of the same generalist as a multi-agent diagnosis.
        """
        if max_roles < 1:
            return []
        missing_tokens = _tokens(getattr(diagnosis, "missing_evidence", []))
        query_tokens = _tokens(getattr(case, "query_terms", []))
        preferred = set(getattr(case, "relevant_skills", []))
        high_impact = bool(getattr(case, "high_blast", False)) or str(
            getattr(case, "blast_radius", "") or ""
        ).strip().lower() in {"high", "critical", "wide", "large", "global"}

        by_role: dict[str, list[tuple[float, float, str]]] = {role.name: [] for role in roles}
        for skill in self.registry.all():
            spec = skill.spec
            if spec.name in executed or spec.frozen or spec.risk != "read_only":
                continue
            skill_tokens = _tokens([spec.name, spec.description, *spec.tags])
            role = max(
                roles,
                key=lambda candidate: (len(skill_tokens & candidate.tokens), -roles.index(candidate)),
            )
            role_overlap = len(skill_tokens & role.tokens)
            relevance = self.MISSING_EVIDENCE_WEIGHT * len(missing_tokens & skill_tokens)
            relevance += self.QUERY_TERM_WEIGHT * len(query_tokens & skill_tokens)
            if spec.name in preferred:
                relevance += self.PREFERRED_BONUS
            # High-impact cases deliberately broaden the read-only evidence fan-out.
            # Otherwise an entirely unrelated fallback is considered only when no
            # positively scored skill exists, matching ``propose`` semantics.
            score = relevance + (0.25 * role_overlap)
            by_role[role.name].append((score, spec.cost, spec.name))

        positive_exists = any(score > 0 for items in by_role.values() for score, _, _ in items)
        role_heads: list[tuple[float, str]] = []
        ordered_by_role: dict[str, list[tuple[float, float, str]]] = {}
        for role_name, items in by_role.items():
            eligible = items if high_impact or not positive_exists else [item for item in items if item[0] > 0]
            ordered = sorted(eligible, key=lambda item: (-item[0], item[1], item[2]))
            if ordered:
                ordered_by_role[role_name] = ordered
                role_heads.append((ordered[0][0], role_name))

        selected_roles = [
            name
            for _, name in sorted(role_heads, key=lambda item: (-item[0], item[1]))[:max_roles]
        ]
        budget = self.batch_size
        assigned: dict[str, list[str]] = {name: [] for name in selected_roles}
        # Diversity pass.
        for role_name in selected_roles:
            if budget <= 0:
                break
            assigned[role_name].append(ordered_by_role[role_name][0][2])
            budget -= 1
        # Depth pass, round-robin across the selected specialists.
        depth = 1
        while budget > 0:
            progressed = False
            for role_name in selected_roles:
                ordered = ordered_by_role[role_name]
                if depth < len(ordered) and budget > 0:
                    assigned[role_name].append(ordered[depth][2])
                    budget -= 1
                    progressed = True
            if not progressed:
                break
            depth += 1

        head_scores = {name: score for score, name in role_heads}
        plan = [
            RoleAssignment(role=name, skill_names=tuple(assigned[name]), score=head_scores[name])
            for name in selected_roles
            if assigned[name]
        ]
        self._record(
            case.id,
            "planner_proposed",
            {
                "mode": "parallel_specialists",
                "assignments": [
                    {"role": item.role, "skills": list(item.skill_names), "score": round(item.score, 4)}
                    for item in plan
                ],
                "skills": [skill for item in plan for skill in item.skill_names],
                "missing_evidence": list(getattr(diagnosis, "missing_evidence", [])),
                "executed": sorted(executed),
                "max_roles": max_roles,
            },
        )
        return plan


class ExecutorAgent:
    """Run proposed read-only skills and append their evidence.

    Enforces the read-only gate at both layers: a non-read-only spec is refused
    before execution, and a handler that *returns* a non-read-only result is
    blocked after the fact. Both raise PermissionError, traced first.
    """

    def __init__(self, *, record: TraceRecorder | None = None):
        self._record = record or _noop_record

    def run(self, case: Any, skill_names: list[str], registry: SkillRegistry) -> tuple[list[dict], float]:
        """Execute `skill_names` in order; returns (collected evidence, total cost).

        Raises KeyError for an unknown skill and PermissionError on any
        read-only violation. Emits `tool_called` + `executor_ran` per skill.
        """
        evidence: list[dict] = []
        total_cost = 0.0
        for name in skill_names:
            skill = registry.get(name)
            if skill.spec.risk != "read_only":
                self._record(
                    case.id,
                    "executor_ran",
                    {
                        "skill": skill.spec.name,
                        "readonly": False,
                        "blocked": True,
                        "reason": "non_readonly_skill",
                    },
                )
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")

            result = registry.execute(skill.spec.name, case=case)
            payload = {
                "skill": skill.spec.name,
                "readonly": result.readonly,
                "evidence_ids": result.evidence_ids(),
                "cost": result.cost,
            }
            if not result.readonly:
                self._record(case.id, "tool_called", {**payload, "blocked": True, "escalation": True})
                self._record(case.id, "executor_ran", {**payload, "blocked": True, "reason": "non_readonly_result"})
                raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")

            total_cost += result.cost
            evidence.extend(result.evidence)
            self._record(case.id, "tool_called", {**payload, "escalation": True})
            self._record(case.id, "executor_ran", {**payload, "blocked": False})
        return evidence, total_cost


class ParallelExecutorAgent:
    """Execute independent read-only specialist assignments concurrently.

    Every skill is preflighted before the pool starts.  Worker threads only
    collect immutable results; trace persistence happens afterwards on the
    caller thread in deterministic role order.
    """

    def __init__(self, *, record: TraceRecorder | None = None):
        self._record = record or _noop_record

    def run(
        self,
        case: Any,
        assignments: list[RoleAssignment],
        registry: SkillRegistry,
        *,
        max_workers: int,
    ) -> tuple[list[dict], float, list[RoleFinding]]:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if not assignments:
            return [], 0.0, []

        # Fail closed before any concurrent handler is allowed to run.
        for assignment in assignments:
            for name in assignment.skill_names:
                skill = registry.get(name)
                if skill.spec.risk != "read_only":
                    self._record(
                        case.id,
                        "executor_ran",
                        {
                            "role": assignment.role,
                            "skill": name,
                            "readonly": False,
                            "blocked": True,
                            "reason": "non_readonly_skill",
                            "mode": "parallel_specialists",
                        },
                    )
                    raise PermissionError(f"non-readonly skill blocked: {name}")

        workers = min(max_workers, len(assignments))
        findings_by_role: dict[str, RoleFinding] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="diagnostic-role") as pool:
            future_roles = {
                pool.submit(self._run_role, case, assignment, registry): assignment.role
                for assignment in assignments
            }
            for future in as_completed(future_roles):
                role = future_roles[future]
                try:
                    findings_by_role[role] = future.result()
                except Exception as exc:  # captured so every role has an auditable terminal event
                    assignment = next(item for item in assignments if item.role == role)
                    findings_by_role[role] = RoleFinding(
                        role=role,
                        skill_names=list(assignment.skill_names),
                        error=f"{type(exc).__name__}: {exc}",
                    )

        ordered_findings = [findings_by_role[item.role] for item in assignments]
        evidence: list[dict] = []
        total_cost = 0.0
        first_error: str | None = None
        for finding in ordered_findings:
            blocked = finding.error is not None
            evidence_ids = [str(item.get("evidence_id")) for item in finding.evidence]
            for name in finding.skill_names:
                skill_evidence = [
                    item for item in finding.evidence if item.get("_diagnostic_skill") == name
                ]
                self._record(
                    case.id,
                    "tool_called",
                    {
                        "skill": name,
                        "role": finding.role,
                        "readonly": not blocked,
                        "evidence_ids": [str(item.get("evidence_id")) for item in skill_evidence],
                        "escalation": True,
                        "parallel": workers > 1,
                        "blocked": blocked,
                    },
                )
            self._record(
                case.id,
                "executor_ran",
                {
                    "role": finding.role,
                    "skills": finding.skill_names,
                    "readonly": not blocked,
                    "blocked": blocked,
                    "error": finding.error,
                    "evidence_ids": evidence_ids,
                    "cost": finding.cost,
                    "duration_ms": round(finding.duration_ms, 3),
                    "worker_thread": finding.worker_thread,
                    "parallel_workers": workers,
                    "mode": "parallel_specialists",
                },
            )
            if blocked:
                first_error = first_error or finding.error
                continue
            for item in finding.evidence:
                cleaned = dict(item)
                cleaned.pop("_diagnostic_skill", None)
                cleaned.setdefault("diagnostic_role", finding.role)
                evidence.append(cleaned)
            total_cost += finding.cost

        if first_error is not None:
            raise PermissionError(f"parallel specialist execution blocked: {first_error}")
        return evidence, total_cost, ordered_findings

    @staticmethod
    def _run_role(case: Any, assignment: RoleAssignment, registry: SkillRegistry) -> RoleFinding:
        finding = RoleFinding(
            role=assignment.role,
            skill_names=list(assignment.skill_names),
            started_at=time.perf_counter(),
            worker_thread=threading.get_ident(),
        )
        try:
            for name in assignment.skill_names:
                result = registry.execute(name, case=case)
                evidence_ids = result.evidence_ids()
                if not result.readonly:
                    raise PermissionError(f"non-readonly tool result blocked: {name}")
                finding.cost += result.cost
                for item in result.evidence:
                    annotated = dict(item)
                    annotated["_diagnostic_skill"] = name
                    finding.evidence.append(annotated)
                # Force identity validation even when no caller consumes the ids.
                _ = evidence_ids
            return finding
        finally:
            finding.finished_at = time.perf_counter()


class CriticAgent:
    """Rebuild context, re-diagnose, verify, and decide whether escalation is resolved."""

    def __init__(self, *, confidence_threshold: float = 0.6, record: TraceRecorder | None = None):
        self.confidence_threshold = confidence_threshold
        self._record = record or _noop_record

    def review(
        self,
        case: Any,
        evidence: list[dict],
        context_compiler: Any,
        diagnosis_builder: Callable[..., Any],
        verifier: Any,
        memories: dict[str, list],
        role_findings: list[RoleFinding] | None = None,
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Return (diagnosis, report, verdict) for the accumulated evidence.

        The verdict passes only if the verifier passes AND confidence meets the
        threshold; emits `context_compiled`, `verifier_result`,
        `diagnosis_completed`, and `critic_reviewed` trace events.
        """
        merged_evidence, conflicts = self._merge_role_evidence(evidence, role_findings or [])
        context = context_compiler.compile(
            case_id=case.id,
            query=case.query,
            memories_by_tier=memories,
            current_evidence=merged_evidence,
            required_evidence=[],
        )
        self._record(case.id, "context_compiled", context.model_dump(mode="json"))

        diagnosis = diagnosis_builder(case=case, evidence=merged_evidence, context=context)
        report = verifier.verify(diagnosis, merged_evidence, [])
        insufficient = (
            not merged_evidence
            or not getattr(diagnosis, "evidence", [])
            or str(getattr(diagnosis, "root_cause_key", "") or "").lower() in {"", "unknown"}
            or bool(getattr(diagnosis, "missing_evidence", []))
        )
        review_errors = list(getattr(report, "errors", []))
        if conflicts:
            review_errors.append(f"cross-agent evidence conflict: {conflicts}")
        if insufficient:
            review_errors.append("insufficient evidence for a grounded diagnosis")
        if review_errors != list(getattr(report, "errors", [])):
            report = VerificationReport(
                passed=False,
                errors=review_errors,
                evidence_recall=float(getattr(report, "evidence_recall", 0.0)),
            )
        self._record(case.id, "verifier_result", report.model_dump(mode="json"))
        self._record(case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))

        confidence = float(getattr(diagnosis, "confidence", 0.0))
        passed = (
            bool(getattr(report, "passed", False))
            and confidence >= self.confidence_threshold
            and not conflicts
            and not insufficient
        )
        verdict = {
            "passed": passed,
            "continue": not passed,
            "verifier_passed": bool(getattr(report, "passed", False)),
            "confidence": confidence,
            "confidence_threshold": self.confidence_threshold,
            "conflicts": conflicts,
            "insufficient_evidence": insufficient,
            "specialist_roles": [finding.role for finding in role_findings or []],
        }
        self._record(case.id, "critic_reviewed", verdict)
        return diagnosis, report, verdict

    @staticmethod
    def _merge_role_evidence(
        evidence: list[dict],
        role_findings: list[RoleFinding],
    ) -> tuple[list[dict], list[str]]:
        """Deduplicate evidence and surface incompatible specialist assertions."""
        merged: list[dict] = []
        by_id: dict[str, dict] = {}
        conflicts: list[str] = []
        assertions: dict[str, tuple[Any, str]] = {}
        role_by_id = {
            str(item.get("evidence_id")): finding.role
            for finding in role_findings
            for item in finding.evidence
            if item.get("evidence_id")
        }
        for item in evidence:
            evidence_id = str(item.get("evidence_id") or "")
            if not evidence_id:
                conflicts.append("evidence_without_id")
                continue
            cleaned = dict(item)
            prior = by_id.get(evidence_id)
            if prior is not None:
                # Specialist provenance is metadata, not part of the observed
                # fact.  The same fact routed through two roles is deduplicated;
                # only a substantive payload mismatch is a conflict.
                comparable_prior = {
                    key: value for key, value in prior.items()
                    if key not in {"diagnostic_role", "_diagnostic_skill"}
                }
                comparable_cleaned = {
                    key: value for key, value in cleaned.items()
                    if key not in {"diagnostic_role", "_diagnostic_skill"}
                }
                if comparable_prior != comparable_cleaned:
                    conflicts.append(f"duplicate_id:{evidence_id}")
                continue
            by_id[evidence_id] = cleaned
            merged.append(cleaned)

            claim_key = cleaned.get("claim_key")
            if claim_key:
                claim_value = cleaned.get("claim_value", cleaned.get("value", cleaned.get("data")))
                role = str(cleaned.get("diagnostic_role") or role_by_id.get(evidence_id) or "unknown")
                previous = assertions.get(str(claim_key))
                if previous is not None and previous[0] != claim_value:
                    conflicts.append(f"claim:{claim_key}:{previous[1]}!={role}")
                else:
                    assertions[str(claim_key)] = (claim_value, role)
        return merged, sorted(set(conflicts))


def _tokens(values: Any) -> set[str]:
    """Lower-cased alphanumeric tokens from a string or iterable of stringables."""
    if isinstance(values, str):
        values = [values]
    return {
        token.lower()
        for value in values or []
        for token in re.findall(r"[A-Za-z0-9]+", str(value).replace("_", " "))
    }
