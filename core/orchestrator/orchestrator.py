from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from core.trace.ledger import JSONLTraceLedger
from core.verifier.verifier import Verifier, VerificationReport


class CaseLike(Protocol):
    """Structural contract every diagnosable case must satisfy (domain schemas do)."""

    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]


class SingleAgentRCAOrchestrator:
    """Single-agent online path; all learning hooks consume trace later.

    Every step is recorded to the trace ledger; any non-read-only skill or skill
    result aborts the run with PermissionError (recorded as a blocked tool call).
    """

    # Episodic recall proposes a prior incident as a hypothesis only.  Historical
    # evidence is never accepted as evidence about the current incident; every run
    # still performs fresh read-only probes before a remembered root cause can be
    # confirmed.
    EPISODIC_RECALL_MIN_CONFIDENCE = 0.9
    EPISODIC_RECALL_MIN_OVERLAP = 0.8
    # procedural shortcut: narrow probing to remembered skills only on strong, proven patterns
    PROCEDURAL_SHORTCUT_MIN_OVERLAP = 0.6
    PROCEDURAL_SHORTCUT_MIN_CONFIDENCE = 1.4

    def __init__(
        self,
        memory: TieredMemoryStore,
        context_compiler: ContextCompiler,
        skills: SkillRegistry,
        skill_controller: SkillAttentionController,
        verifier: Verifier,
        diagnosis_builder: Callable[..., Any],
        ledger_path: str | Path,
    ):
        self.memory = memory
        self.context_compiler = context_compiler
        self.skills = skills
        self.skill_controller = skill_controller
        self.verifier = verifier
        self.diagnosis_builder = diagnosis_builder
        self.ledger = JSONLTraceLedger(ledger_path)
        self._run_events: list[TraceEvent] = []
        self._last_evidence: list[dict] = []
        self.last_run_id: str = ""

    def diagnose(self, case: CaseLike) -> tuple[Any, VerificationReport]:
        """Run one read-only diagnosis for `case`.

        Returns (diagnosis, verification report). Raises PermissionError when a
        non-read-only skill or result is encountered; the block is traced first.
        """
        run_id = str(uuid4())
        self._run_events = []
        self.last_run_id = run_id
        self._record(run_id, case.id, "alert_received", {"query": case.query, "assets": case.assets})

        memories = self.memory.retrieve(case.query_terms, case.assets)
        self._record(
            run_id,
            case.id,
            "memory_read",
            {tier: [record.memory_id for record in records] for tier, records in memories.items()},
        )

        query = {t.lower() for t in case.query_terms}
        recalled = self._recall_episodic(memories.get("episodic", []), query, case.assets)
        # Even a strong episodic match is only a prior.  Procedural memory may
        # narrow the probe list, but the evidence passed to the reasoner always
        # comes from tools executed in this run.
        evidence, total_cost = self._probe_with_skills(run_id, case, query, memories.get("procedural", []))

        self._last_evidence = evidence
        context = self.context_compiler.compile(
            case_id=case.id,
            query=case.query,
            memories_by_tier=memories,
            current_evidence=evidence,
            required_evidence=[],
        )
        self._record(run_id, case.id, "context_compiled", context.model_dump(mode="json"))

        diagnosis = self.diagnosis_builder(case=case, evidence=evidence, context=context)
        report = self.verifier.verify(diagnosis, evidence, [])
        self._record(run_id, case.id, "verifier_result", report.model_dump(mode="json"))
        tool_calls = sum(1 for e in self._run_events if e.kind == "tool_called" and not e.payload.get("blocked"))
        remembered_root = self._remembered_root(recalled) if recalled is not None else None
        if (
            recalled is not None
            and remembered_root
            and tool_calls > 0
            and report.passed
            and diagnosis.root_cause_key == remembered_root
        ):
            # ``memory_resolved`` now means "a recalled hypothesis was confirmed
            # by fresh evidence", never "old evidence was replayed as current".
            self._record(
                run_id,
                case.id,
                "memory_resolved",
                {
                    "memory_id": recalled.memory_id,
                    "remembered_root_cause_key": remembered_root,
                    "historical_evidence_ids": [
                        item.get("evidence_id") for item in recalled.evidence_snapshot if item.get("evidence_id")
                    ],
                    "current_evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
                    "fresh_probe_count": tool_calls,
                    "freshness_verified": True,
                    "recalled_confidence": round(recalled.confidence, 3),
                },
            )
        self._record(run_id, case.id, "cost_observed", {"tool_cost": total_cost, "tool_calls": tool_calls})
        self._record(run_id, case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))
        return diagnosis, report

    def _recall_episodic(self, episodic: list[Any], query: set[str], assets: list[str]) -> Any | None:
        """Return the first episodic record strong enough to be a current hypothesis."""
        for record in episodic:
            rec_terms = {t.lower() for t in record.tags}
            overlap = len(query & rec_terms) / len(query) if query else 0.0
            if (
                record.evidence_snapshot
                and record.confidence >= self.EPISODIC_RECALL_MIN_CONFIDENCE
                and overlap >= self.EPISODIC_RECALL_MIN_OVERLAP
                and set(assets) & set(record.asset_ids)
            ):
                return record
        return None

    @staticmethod
    def _remembered_root(record: Any) -> str | None:
        """Read the explicitly stored root key; never infer it from prose."""
        for tag in getattr(record, "tags", []):
            if tag.startswith("root:") and len(tag) > len("root:"):
                return tag[len("root:"):]
        return None

    def _probe_with_skills(
        self,
        run_id: str,
        case: CaseLike,
        query: set[str],
        procedural: list[Any],
    ) -> tuple[list[dict], float]:
        """Select read-only skills (procedural-memory shortcut aware), execute them, trace each call."""
        # procedural-memory shortcut: reuse the skills proven to matter for this
        # recurring pattern. Gated on STRONG query-term overlap so a merely shared
        # asset can't apply the wrong pattern's skills — accuracy is never traded for speed.
        mem_skills: list[str] = []
        best_proc_conf = 0.0
        for record in procedural:
            rec_terms = {t.lower() for t in record.tags if not t.startswith("skill:")}
            overlap = len(query & rec_terms) / len(query) if query else 0.0
            if overlap >= self.PROCEDURAL_SHORTCUT_MIN_OVERLAP:
                best_proc_conf = max(best_proc_conf, record.confidence)
                mem_skills.extend(t[len("skill:"):] for t in record.tags if t.startswith("skill:"))
        mem_skills = list(dict.fromkeys(mem_skills))
        preferred = list(dict.fromkeys(list(case.relevant_skills) + mem_skills))

        selected = self.skill_controller.select(self.skills.all(), case.query_terms, preferred)
        if mem_skills and best_proc_conf >= self.PROCEDURAL_SHORTCUT_MIN_CONFIDENCE:
            named = [skill for skill in selected if skill.spec.name in mem_skills]
            if named and len(named) < len(selected):
                selected = named
                self._record(
                    run_id,
                    case.id,
                    "memory_shortcut",
                    {"skills": [s.spec.name for s in named], "procedural_confidence": round(best_proc_conf, 3)},
                )
        self._record(run_id, case.id, "skills_exposed", {"skills": [s.spec.name for s in selected]})

        evidence: list[dict] = []
        total_cost = 0.0
        for skill in selected:
            if skill.spec.risk != "read_only":
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")
            result = self.skills.execute(skill.spec.name, case=case)
            evidence_ids = result.evidence_ids()
            if not result.readonly:
                self._record(
                    run_id,
                    case.id,
                    "tool_called",
                    {"skill": skill.spec.name, "readonly": result.readonly, "evidence_ids": evidence_ids, "cost": result.cost, "blocked": True},
                )
                raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")
            total_cost += result.cost
            evidence.extend(result.evidence)
            self._record(
                run_id,
                case.id,
                "tool_called",
                {"skill": skill.spec.name, "readonly": result.readonly, "evidence_ids": evidence_ids, "cost": result.cost},
            )
        return evidence, total_cost

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
        self.ledger.append(event)
        self._run_events.append(event)
