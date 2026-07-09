from __future__ import annotations

import json
from pathlib import Path

from core.context.compiler import ContextCompiler
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.verifier.verifier import Verifier
from domains.active_recon.adapters.mock_target import MockTargetAdapter
from domains.active_recon.schema import ReconCase, ReconGroundTruth
from domains.active_recon.situational import build_recon_diagnosis
from domains.active_recon.skills.recon_skills import register_active_recon_skills


ROOT = Path(__file__).resolve().parent


def load_recon_seed_cases(path: str | Path | None = None) -> list[ReconCase]:
    case_path = Path(path) if path else ROOT / "seed_cases" / "seed_cases.json"
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    return [ReconCase.model_validate(item["case"]) for item in raw]


def load_recon_ground_truth(path: str | Path | None = None) -> dict[str, ReconGroundTruth]:
    case_path = Path(path) if path else ROOT / "seed_cases" / "seed_cases.json"
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    truths = [
        ReconGroundTruth.model_validate({"case_id": item["case"]["id"], **item["ground_truth"]})
        for item in raw
    ]
    return {truth.case_id: truth for truth in truths}


def load_recon_memory_records(path: str | Path | None = None) -> list[MemoryRecord]:
    memory_path = Path(path) if path else ROOT / "fixtures" / "memory_seed.json"
    raw = json.loads(memory_path.read_text(encoding="utf-8"))
    return [MemoryRecord.model_validate(item) for item in raw]


def build_active_recon_orchestrator(
    ledger_path: str | Path,
    *,
    memory_enabled: bool = True,
    context_enabled: bool = True,
    skill_controller_enabled: bool = True,
    verifier_enabled: bool = True,
    top_k: int = 3,
    seed_memory: bool = True,
) -> SingleAgentRCAOrchestrator:
    memory = TieredMemoryStore(enabled=memory_enabled)
    if seed_memory:
        memory.seed(load_recon_memory_records())
    registry = SkillRegistry()
    adapter = MockTargetAdapter.from_path(ROOT / "fixtures" / "mock_target_responses.json")
    register_active_recon_skills(registry, adapter)

    return SingleAgentRCAOrchestrator(
        memory=memory,
        context_compiler=ContextCompiler(token_budget=220, enabled=context_enabled),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=skill_controller_enabled, top_k=top_k),
        verifier=Verifier(enabled=verifier_enabled),
        diagnosis_builder=build_recon_diagnosis,
        ledger_path=ledger_path,
    )
