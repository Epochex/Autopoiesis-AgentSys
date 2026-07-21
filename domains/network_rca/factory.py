from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from core.context.compiler import ContextCompiler
from core.env import autopoiesis_env
from core.llm.provider import OpenAICompatibleClient
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.verifier.verifier import Verifier
from domains.network_rca.adapters.mock_device import MockDeviceAdapter
from domains.network_rca.adapters.real_syslog_adapter import RealSyslogAdapter
from domains.network_rca.reasoner import (
    LLMReasoner,
    ROOT_CAUSE_EVIDENCE_CONTRACTS,
    build_diagnosis,
)
from domains.network_rca.schema import RCAGroundTruth, RCASeedCase
from domains.network_rca.skills.network_skills import register_network_rca_skills
from domains.network_rca.skills.real_skills import register_real_rca_skills


ROOT = Path(__file__).resolve().parent


def load_seed_cases(path: str | Path | None = None) -> list[RCASeedCase]:
    case_path = Path(path) if path else ROOT / "seed_cases" / "seed_cases.json"
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    return [RCASeedCase.model_validate(item["case"]) for item in raw]


def load_ground_truth(path: str | Path | None = None) -> dict[str, RCAGroundTruth]:
    case_path = Path(path) if path else ROOT / "seed_cases" / "seed_cases.json"
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    truths = [RCAGroundTruth.model_validate({"case_id": item["case"]["id"], **item["ground_truth"]}) for item in raw]
    return {truth.case_id: truth for truth in truths}


def load_memory_records(path: str | Path | None = None) -> list[MemoryRecord]:
    memory_path = Path(path) if path else ROOT / "fixtures" / "memory_seed.json"
    raw = json.loads(memory_path.read_text(encoding="utf-8"))
    return [MemoryRecord.model_validate(item) for item in raw]


def build_network_rca_orchestrator(
    ledger_path: str | Path,
    *,
    memory_enabled: bool = True,
    context_enabled: bool = True,
    skill_controller_enabled: bool = True,
    verifier_enabled: bool = True,
    top_k: int = 3,
    reasoner_mode: str = "rule",
    llm_client=None,
    data_source: str = "mock",
    real_stats_path: str | Path | None = None,
    seed_memory: bool = True,
    context_strategy: str = "structured",
    memory_dsn: str | None = None,
    vector_memory_enabled: bool | None = None,
    memory_embedder: Any | None = None,
    vector_options: dict[str, Any] | None = None,
    observer: Any | None = None,
    observability_path: str | Path | None = None,
) -> SingleAgentRCAOrchestrator:
    if observer is None:
        from core.observability import ExecutionObserver, LangfuseTraceExporter

        exporters = []
        langfuse_exporter = LangfuseTraceExporter.from_environment()
        if langfuse_exporter is not None:
            exporters.append(langfuse_exporter)
        resolved_observability_path = observability_path or autopoiesis_env(
            "OBSERVABILITY_PATH"
        )
        if resolved_observability_path is None:
            trace_path = Path(ledger_path)
            resolved_observability_path = trace_path.with_name(
                f"{trace_path.stem}.observability.jsonl"
            )
        observer = ExecutionObserver(
            resolved_observability_path,
            exporters=exporters,
        )
    resolved_memory_dsn = memory_dsn or autopoiesis_env("MEMORY_DSN")
    if resolved_memory_dsn:
        from core.memory.postgres_repository import PostgresMemoryRepository

        repository = PostgresMemoryRepository(resolved_memory_dsn)
        repository.initialize_schema()
        memory = TieredMemoryStore.from_repository(repository, enabled=memory_enabled)
        if seed_memory and not memory.records():
            memory.seed(load_memory_records())
            memory.flush()
    else:
        memory = TieredMemoryStore(enabled=memory_enabled)
        if seed_memory:
            memory.seed(load_memory_records())

    resolved_vector_enabled = vector_memory_enabled
    if resolved_vector_enabled is None:
        resolved_vector_enabled = (autopoiesis_env("ENABLE_VECTOR_MEMORY", "0") or "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
    if resolved_vector_enabled:
        from core.memory.vector_memory import (
            BGETextEmbedder,
            VectorMemoryDependencyError,
            VectorMemoryIndex,
        )

        embedder = memory_embedder or BGETextEmbedder()
        documents = {
            record.memory_id: memory.vector_document(record)
            for record in memory.active()
        }
        try:
            memory.attach_vector_index(
                VectorMemoryIndex.build(documents, embedder, **(vector_options or {}))
            )
        except VectorMemoryDependencyError as exc:
            memory.mark_vector_degraded(str(exc))
    registry = SkillRegistry()
    if data_source == "mock":
        adapter = MockDeviceAdapter(ROOT / "fixtures" / "mock_device_responses.json")
        register_network_rca_skills(registry, adapter)
    elif data_source == "real":
        if real_stats_path is None:
            raise ValueError("data_source='real' requires real_stats_path")
        register_real_rca_skills(registry, RealSyslogAdapter.from_path(real_stats_path))
    else:
        raise ValueError(f"unknown data_source: {data_source}")
    if reasoner_mode == "rule":
        diagnosis_builder = build_diagnosis
    elif reasoner_mode == "llm":
        diagnosis_builder = LLMReasoner(llm_client or OpenAICompatibleClient())
    else:
        raise ValueError(f"unknown reasoner_mode: {reasoner_mode}")

    return SingleAgentRCAOrchestrator(
        memory=memory,
        context_compiler=ContextCompiler(
            token_budget=2_048,
            enabled=context_enabled,
            strategy=context_strategy,
            max_memory_lines=24,
            max_evidence_lines=32,
        ),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=skill_controller_enabled, top_k=top_k),
        verifier=Verifier(
            enabled=verifier_enabled,
            evidence_contracts=ROOT_CAUSE_EVIDENCE_CONTRACTS,
        ),
        diagnosis_builder=diagnosis_builder,
        ledger_path=ledger_path,
        observer=observer,
    )


def build_network_rca_service(
    ledger_path: str | Path,
    *,
    maintenance_interval_seconds: float = 60.0,
    start_maintenance: bool = True,
    raise_on_evolution_error: bool = False,
    projection_max_batches: int = 10,
    project_memory_events: bool | None = None,
    projection_index_name: str | None = None,
    projection_batch_size: int = 1_000,
    consolidation_options: dict[str, Any] | None = None,
    **orchestrator_options: Any,
):
    """Build the online RCA path with verified learning and index maintenance.

    The base orchestrator remains available for immutable evaluations.  This is
    the production-facing entry point when a long-lived process should learn
    from verifier-approved runs and compact its sparse/vector indexes off the
    request thread.
    """
    from core.memory.index_projector import MemoryIndexProjector
    from core.orchestrator.evolving_service import EvolvingRCAService

    orchestrator = build_network_rca_orchestrator(ledger_path, **orchestrator_options)
    projector = None
    repository = orchestrator.memory.repository
    projection_enabled = (
        repository is not None
        if project_memory_events is None
        else project_memory_events
    )
    if projection_enabled:
        if repository is None:
            raise ValueError("memory event projection requires PostgreSQL persistence")
        if projection_index_name is None:
            replica_key = hashlib.sha256(
                str(Path(ledger_path).resolve()).encode("utf-8")
            ).hexdigest()[:16]
            projection_index_name = f"network-rca-online-{replica_key}"
        elif not projection_index_name.strip():
            raise ValueError("projection_index_name must not be empty")
        projector = MemoryIndexProjector(
            repository,
            orchestrator.memory,
            index_name=projection_index_name,
            batch_size=projection_batch_size,
        )

    return EvolvingRCAService(
        orchestrator,
        projector=projector,
        maintenance_interval_seconds=maintenance_interval_seconds,
        start_maintenance=start_maintenance,
        raise_on_evolution_error=raise_on_evolution_error,
        projection_max_batches=projection_max_batches,
        consolidation_options=consolidation_options,
    )
