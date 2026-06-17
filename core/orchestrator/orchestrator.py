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

    def diagnose(self, case) -> tuple[object, object]:
        run_id = str(uuid4())
        self._record(run_id, case.id, "alert_received", {"query": case.query, "assets": case.assets})

        memories = self.memory.retrieve(case.query_terms, case.assets)
        self._record(
            run_id,
            case.id,
            "memory_read",
            {tier: [record.memory_id for record in records] for tier, records in memories.items()},
        )

        selected = self.skill_controller.select(self.skills.all(), case.query_terms, case.relevant_skills)
        exposed_names = [skill.spec.name for skill in selected]
        self._record(run_id, case.id, "skills_exposed", {"skills": exposed_names})

        evidence: list[dict] = []
        total_cost = 0.0
        for skill in selected:
            if skill.spec.risk != "read_only":
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")
            result = self.skills.execute(skill.spec.name, case=case)
            if not result.readonly:
                self._record(
                    run_id,
                    case.id,
                    "tool_called",
                    {
                        "skill": skill.spec.name,
                        "readonly": result.readonly,
                        "evidence_ids": [item["evidence_id"] for item in result.evidence],
                        "cost": result.cost,
                        "blocked": True,
                    },
                )
                raise PermissionError(f"non-readonly tool result blocked: {skill.spec.name}")
            total_cost += result.cost
            evidence.extend(result.evidence)
            self._record(
                run_id,
                case.id,
                "tool_called",
                {
                    "skill": skill.spec.name,
                    "readonly": result.readonly,
                    "evidence_ids": [item["evidence_id"] for item in result.evidence],
                    "cost": result.cost,
                },
            )

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
        self._record(run_id, case.id, "cost_observed", {"tool_cost": total_cost, "tool_calls": len(selected)})
        self._record(run_id, case.id, "diagnosis_completed", diagnosis.model_dump(mode="json"))
        return diagnosis, report

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        self.ledger.append(TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload))
