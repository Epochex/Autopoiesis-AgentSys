from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from core.trace.ledger import JSONLTraceLedger
from core.verifier.verifier import Verifier


class SingleAgentRCAOrchestrator:
    """Single-agent online path; all learning hooks consume trace later."""

    def __init__(
        self,
        memory: TieredMemoryStore,
        context_compiler: ContextCompiler,
        skills: SkillRegistry,
        skill_controller: SkillAttentionController,
        verifier: Verifier,
        diagnosis_builder,
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

    def diagnose(self, case) -> tuple[object, object]:
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
        evidence: list[dict] = []
        total_cost = 0.0

        # episodic recall: a strongly-matching prior incident lets us reuse its observed,
        # provenance-linked evidence and skip fresh probing entirely — the reasoner still
        # re-derives the verdict and the verifier still checks every citation was observed.
        recalled = None
        for record in memories.get("episodic", []):
            rec_terms = {t.lower() for t in record.tags}
            overlap = len(query & rec_terms) / len(query) if query else 0.0
            if (record.evidence_snapshot and record.confidence >= 0.9
                    and overlap >= 0.8 and set(case.assets) & set(record.asset_ids)):
                recalled = record
                break

        if recalled is not None:
            evidence = [dict(item) for item in recalled.evidence_snapshot]
            self._record(run_id, case.id, "memory_resolved", {"memory_id": recalled.memory_id, "evidence_ids": [e.get("evidence_id") for e in evidence], "recalled_confidence": round(recalled.confidence, 3)})
            self._record(run_id, case.id, "skills_exposed", {"skills": []})
        else:
            # procedural-memory shortcut: reuse the skills proven to matter for this
            # recurring pattern. Gated on STRONG query-term overlap so a merely shared
            # asset can't apply the wrong pattern's skills — accuracy is never traded for speed.
            mem_skills: list[str] = []
            best_proc_conf = 0.0
            for record in memories.get("procedural", []):
                rec_terms = {t.lower() for t in record.tags if not t.startswith("skill:")}
                overlap = len(query & rec_terms) / len(query) if query else 0.0
                if overlap >= 0.6:
                    best_proc_conf = max(best_proc_conf, record.confidence)
                    mem_skills.extend(t[len("skill:"):] for t in record.tags if t.startswith("skill:"))
            mem_skills = list(dict.fromkeys(mem_skills))
            preferred = list(dict.fromkeys(list(case.relevant_skills) + mem_skills))

            selected = self.skill_controller.select(self.skills.all(), case.query_terms, preferred)
            if mem_skills and best_proc_conf >= 1.4:
                named = [skill for skill in selected if skill.spec.name in mem_skills]
                if named and len(named) < len(selected):
                    selected = named
                    self._record(run_id, case.id, "memory_shortcut", {"skills": [s.spec.name for s in named], "procedural_confidence": round(best_proc_conf, 3)})
            self._record(run_id, case.id, "skills_exposed", {"skills": [s.spec.name for s in selected]})

            for skill in selected:
                if skill.spec.risk != "read_only":
                    raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")
                result = self.skills.execute(skill.spec.name, case=case)
                if not result.readonly:
                    self._record(run_id, case.id, "tool_called", {"skill": skill.spec.name, "readonly": result.readonly, "evidence_ids": [item["evidence_id"] for item in result.evidence], "cost": result.cost, "blocked": True})
                    raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")
                total_cost += result.cost
                evidence.extend(result.evidence)
                self._record(run_id, case.id, "tool_called", {"skill": skill.spec.name, "readonly": result.readonly, "evidence_ids": [item["evidence_id"] for item in result.evidence], "cost": result.cost})

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
        self._record(run_id, case.id, "cost_observed", {"tool_cost": total_cost, "tool_calls": tool_calls})
        self._record(run_id, case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))
        return diagnosis, report

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
        self.ledger.append(event)
        self._run_events.append(event)
