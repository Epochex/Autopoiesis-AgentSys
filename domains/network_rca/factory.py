from __future__ import annotations

import json
from pathlib import Path

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
from domains.network_rca.reasoner import LLMReasoner, build_diagnosis
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
) -> SingleAgentRCAOrchestrator:
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
            token_budget=220,
            enabled=context_enabled,
            strategy=context_strategy,
        ),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=skill_controller_enabled, top_k=top_k),
        verifier=Verifier(enabled=verifier_enabled),
        diagnosis_builder=diagnosis_builder,
        ledger_path=ledger_path,
    )
