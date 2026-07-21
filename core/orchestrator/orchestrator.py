from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.memory.evolution import analyze_evolution
from core.observability.observer import ExecutionObserver
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
        observer: ExecutionObserver | None = None,
    ):
        self.memory = memory
        self.context_compiler = context_compiler
        self.skills = skills
        self.skill_controller = skill_controller
        self.verifier = verifier
        self.diagnosis_builder = diagnosis_builder
        self.ledger = JSONLTraceLedger(ledger_path)
        ledger_path = Path(ledger_path)
        observation_path = ledger_path.with_name(
            f"{ledger_path.stem}.observability.jsonl"
        )
        self.observer = observer or ExecutionObserver(observation_path)
        self._run_events: list[TraceEvent] = []
        self._last_evidence: list[dict] = []
        self.last_run_id: str = ""
        self._current_session_id: str | None = None

    def diagnose(
        self,
        case: CaseLike,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        observe_root: bool = True,
    ) -> tuple[Any, VerificationReport]:
        """Run one read-only diagnosis for `case`.

        Returns (diagnosis, verification report). Raises PermissionError when a
        non-read-only skill or result is encountered; the block is traced first.
        """
        run_id = run_id or str(uuid4())
        if not observe_root:
            return self._diagnose(case, run_id=run_id, session_id=session_id)
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="rca.diagnose",
            node_type="workflow",
            input={"query": case.query, "assets": case.assets},
        ) as root_span:
            diagnosis, report = self._diagnose(
                case, run_id=run_id, session_id=session_id
            )
            root_span.set_result(
                output={
                    "root_cause_key": diagnosis.root_cause_key,
                    "verified": report.passed,
                },
                metrics={
                    "verification_passed": report.passed,
                    "evidence_cited": len(diagnosis.evidence),
                },
                status="ok" if report.passed else "partial",
            )
            return diagnosis, report

    def _diagnose(
        self,
        case: CaseLike,
        *,
        run_id: str,
        session_id: str | None,
    ) -> tuple[Any, VerificationReport]:
        self._run_events = []
        self.last_run_id = run_id
        self._current_session_id = session_id
        self._record(run_id, case.id, "alert_received", {"query": case.query, "assets": case.assets})

        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="memory.retrieve",
            node_type="retrieval",
            input={"query_terms": case.query_terms, "assets": case.assets},
            attributes={"limit_per_tier": 6, "graph_depth": 2},
        ) as retrieval_span:
            memories = self.memory.retrieve(
                case.query_terms,
                case.assets,
                limit_per_tier=6,
                graph_depth=2,
                graph_candidate_limit=48,
            )
            retrieval_candidates = self.memory.retrieval_diagnostics()
            retrieval_span.set_result(
                output={
                    "memory_ids_by_tier": {
                        tier: [record.memory_id for record in records]
                        for tier, records in memories.items()
                    }
                },
                metrics={
                    "candidate_count": len(retrieval_candidates),
                    "returned_count": sum(len(records) for records in memories.values()),
                    "lexical_hits": sum(
                        float(item.get("lexical_score", 0.0)) > 0.0
                        for item in retrieval_candidates
                    ),
                    "asset_hits": sum(
                        int(item.get("asset_hits", 0)) > 0
                        for item in retrieval_candidates
                    ),
                    "vector_hits": sum(
                        float(item.get("vector_score", 0.0)) > 0.0
                        for item in retrieval_candidates
                    ),
                    "graph_hits": sum(
                        int(item.get("graph_hop", 0)) > 0
                        for item in retrieval_candidates
                    ),
                },
                attributes={"index_health": self.memory.index_health()},
            )
        self._record(
            run_id,
            case.id,
            "memory_read",
            {tier: [record.memory_id for record in records] for tier, records in memories.items()},
        )
        self._record(
            run_id,
            case.id,
            "memory_candidates_ranked",
            {"candidates": retrieval_candidates},
        )
        recalled_records = [record for records in memories.values() for record in records]
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="memory.evolution.analyze",
            node_type="analysis",
            input={"memory_ids": [record.memory_id for record in recalled_records]},
        ) as evolution_span:
            evolution = analyze_evolution(recalled_records, current_assets=case.assets)
            evolution_span.set_result(
                output={
                    "chain": list(evolution.chain.memory_ids) if evolution else [],
                    "relation_types": list(evolution.relation_types) if evolution else [],
                    "hidden_failure_pattern": bool(
                        evolution and evolution.hidden_failure_pattern
                    ),
                },
                metrics={
                    "chain_length": len(evolution.chain.memory_ids) if evolution else 0,
                    "verified": bool(evolution and evolution.verified),
                    "hidden_failure_pattern": bool(
                        evolution and evolution.hidden_failure_pattern
                    ),
                },
            )
        if evolution is not None:
            self._record(
                run_id,
                case.id,
                "system_evolution_analyzed",
                {
                    "memory_ids": list(evolution.chain.memory_ids),
                    "early_change_id": evolution.chain.early_change_id,
                    "relation_types": list(evolution.relation_types),
                    "asset_path": list(evolution.asset_path),
                    "hidden_failure_pattern": evolution.hidden_failure_pattern,
                    "verified": evolution.verified,
                },
            )

        query = {t.lower() for t in case.query_terms}
        recalled = self._recall_episodic(memories.get("episodic", []), query, case.assets)
        # Even a strong episodic match is only a prior.  Procedural memory may
        # narrow the probe list, but the evidence passed to the reasoner always
        # comes from tools executed in this run.
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="skills.probe",
            node_type="agent",
            input={"preferred_skills": case.relevant_skills},
        ) as probe_span:
            evidence, total_cost, procedural_attribution = self._probe_with_skills(
                run_id,
                case,
                query,
                memories.get("procedural", []),
                session_id=session_id,
            )
            probe_span.set_result(
                output={
                    "evidence_ids": [item.get("evidence_id") for item in evidence],
                    "procedural_attribution": procedural_attribution,
                },
                metrics={"evidence_count": len(evidence), "tool_cost": total_cost},
            )

        self._last_evidence = evidence
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="context.compile",
            node_type="context",
            input={
                "retrieved_memories": sum(len(items) for items in memories.values()),
                "current_evidence": len(evidence),
            },
        ) as context_span:
            context = self.context_compiler.compile(
                case_id=case.id,
                query=case.query,
                memories_by_tier=memories,
                current_evidence=evidence,
                required_evidence=[],
            )
            dropped = sum(len(section.dropped) for section in context.sections)
            context_span.set_result(
                output={
                    "included_memory_ids": context.included_memory_ids,
                    "included_evidence_ids": context.included_evidence_ids,
                    "missing_evidence": context.missing_evidence,
                },
                metrics={
                    "tokens_before": context.estimated_tokens_before,
                    "tokens_after": context.estimated_tokens_after,
                    "compression_ratio": context.compression_ratio,
                    "included_memories": len(context.included_memory_ids),
                    "included_evidence": len(context.included_evidence_ids),
                    "dropped_items": dropped,
                },
                attributes={"compiler_mode": context.compiler_mode},
            )
        self._record(run_id, case.id, "context_compiled", context.model_dump(mode="json"))

        # Attribution is narrower than retrieval: only memories that affected this
        # run are named, and consolidation must still intersect these ids with the
        # compiler's included_memory_ids.  This prevents a merely retrieved or
        # subsequently dropped candidate from receiving credit or blame.
        included_memory_ids = set(context.included_memory_ids)
        attribution: list[dict[str, str]] = []
        if recalled is not None and recalled.memory_id in included_memory_ids:
            attribution.append({"memory_id": recalled.memory_id, "role": "episodic_hypothesis"})
        attribution.extend(
            {"memory_id": memory_id, "role": "procedural_shortcut"}
            for memory_id in procedural_attribution
            if memory_id in included_memory_ids
        )
        if attribution:
            self._record(
                run_id,
                case.id,
                "memory_attributed",
                {"items": attribution, "memory_ids": [item["memory_id"] for item in attribution]},
            )

        reasoner_name = type(self.diagnosis_builder).__name__
        if reasoner_name == "function":
            reasoner_name = getattr(self.diagnosis_builder, "__name__", reasoner_name)
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="reasoner.diagnose",
            node_type="llm" if reasoner_name == "LLMReasoner" else "reasoner",
            input={
                "context_tokens": context.estimated_tokens_after,
                "included_evidence_ids": context.included_evidence_ids,
            },
            attributes={"reasoner": reasoner_name},
        ) as reasoner_span:
            diagnosis = self.diagnosis_builder(case=case, evidence=evidence, context=context)
            reasoner_span.set_result(
                output={
                    "root_cause_key": diagnosis.root_cause_key,
                    "evidence_ids": [item.evidence_id for item in diagnosis.evidence],
                    "missing_evidence": diagnosis.missing_evidence,
                },
                metrics={
                    "confidence": diagnosis.confidence,
                    "evidence_cited": len(diagnosis.evidence),
                },
            )
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="verifier.verify",
            node_type="verifier",
            input={
                "root_cause_key": diagnosis.root_cause_key,
                "evidence_ids": [item.evidence_id for item in diagnosis.evidence],
            },
        ) as verifier_span:
            report = self.verifier.verify(diagnosis, evidence, [])
            verifier_span.set_result(
                output={"passed": report.passed, "errors": list(report.errors)},
                metrics={"passed": report.passed, "error_count": len(report.errors)},
                status="ok" if report.passed else "partial",
            )
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
        *,
        session_id: str | None = None,
    ) -> tuple[list[dict], float, list[str]]:
        """Select read-only skills (procedural-memory shortcut aware), execute them, trace each call."""
        # procedural-memory shortcut: reuse the skills proven to matter for this
        # recurring pattern. Gated on STRONG query-term overlap so a merely shared
        # asset can't apply the wrong pattern's skills — accuracy is never traded for speed.
        mem_skills: list[str] = []
        best_proc_conf = 0.0
        procedure_skill_names: dict[str, set[str]] = {}
        for record in procedural:
            rec_terms = {t.lower() for t in record.tags if not t.startswith("skill:")}
            overlap = len(query & rec_terms) / len(query) if query else 0.0
            if overlap >= self.PROCEDURAL_SHORTCUT_MIN_OVERLAP:
                best_proc_conf = max(best_proc_conf, record.confidence)
                tagged_skills = {t[len("skill:"):] for t in record.tags if t.startswith("skill:")}
                mem_skills.extend(tagged_skills)
                if record.confidence >= self.PROCEDURAL_SHORTCUT_MIN_CONFIDENCE:
                    procedure_skill_names[record.memory_id] = tagged_skills
        mem_skills = list(dict.fromkeys(mem_skills))
        preferred = list(dict.fromkeys(list(case.relevant_skills) + mem_skills))

        selected = self.skill_controller.select(self.skills.all(), case.query_terms, preferred)
        attributed_procedures: list[str] = []
        if mem_skills and best_proc_conf >= self.PROCEDURAL_SHORTCUT_MIN_CONFIDENCE:
            named = [skill for skill in selected if skill.spec.name in mem_skills]
            if named and len(named) < len(selected):
                selected = named
                selected_names = {skill.spec.name for skill in named}
                attributed_procedures = [
                    memory_id
                    for memory_id, skill_names in procedure_skill_names.items()
                    if selected_names & skill_names
                ]
                self._record(
                    run_id,
                    case.id,
                    "memory_shortcut",
                    {
                        "skills": [s.spec.name for s in named],
                        "memory_ids": attributed_procedures,
                        "procedural_confidence": round(best_proc_conf, 3),
                    },
                )
        self._record(run_id, case.id, "skills_exposed", {"skills": [s.spec.name for s in selected]})

        evidence: list[dict] = []
        total_cost = 0.0
        for skill in selected:
            if skill.spec.risk != "read_only":
                raise PermissionError(f"non-readonly skill blocked: {skill.spec.name}")
            with self.observer.span(
                trace_id=run_id,
                session_id=session_id,
                case_id=case.id,
                node_name=f"tool.{skill.spec.name}",
                node_type="tool",
                input={"case_id": case.id, "assets": case.assets},
                attributes={
                    "risk": skill.spec.risk,
                    "expected_cost": skill.spec.cost,
                },
            ) as tool_span:
                result = self.skills.execute(skill.spec.name, case=case)
                evidence_ids = result.evidence_ids()
                tool_span.set_result(
                    output={
                        "evidence_ids": evidence_ids,
                        "readonly": result.readonly,
                    },
                    metrics={
                        "cost": result.cost,
                        "evidence_count": len(evidence_ids),
                    },
                    status="ok" if result.readonly else "error",
                )
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
        return evidence, total_cost, attributed_procedures

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict) -> None:
        event = TraceEvent(run_id=run_id, case_id=case_id, kind=kind, payload=payload)
        with self.observer.span(
            trace_id=run_id,
            session_id=self._current_session_id,
            case_id=case_id,
            node_name=f"trace.persist.{kind}",
            node_type="storage",
            input={"event_kind": kind},
            attributes={"sync_boundary": self.ledger.is_sync_boundary(kind)},
        ) as persistence_span:
            self.ledger.append(event)
            persistence_span.set_result(
                output={"event_id": event.event_id},
                metrics={
                    "payload_bytes": len(
                        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                    ),
                    "fsync": self.ledger.is_sync_boundary(kind),
                },
            )
        self._run_events.append(event)
