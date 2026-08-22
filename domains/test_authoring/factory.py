from __future__ import annotations

from pathlib import Path
from typing import Any

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.orchestrator.intent_router import CascadingIntentRouter
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.verifier.contracts import ContractVerifier
from core.verifier.verifier import Verifier
from domains.test_authoring.adapters.spec_library import SpecLibrary
from domains.test_authoring.schema import SpecUnderTest, TestAuthoringCase
from domains.test_authoring.skills.authoring_skills import register_test_authoring_skills


ROOT = Path(__file__).resolve().parent
SPECS_PATH = ROOT / "fixtures" / "specs.json"


def load_test_authoring_seed_cases() -> list[TestAuthoringCase]:
    """Seed requests, one per spec, together covering all five analysis skills.

    The queries carry the English skill vocabulary because the kernel's rule
    parser tokenises on it — the parameters of a real API are English anyway, so
    this is how an operator would actually type the request.
    """
    return [
        TestAuthoringCase(
            id="authoring_cost_boundaries",
            query="analyze signature, then find boundaries",
            query_terms=["analyze", "signature", "find", "boundaries", "test", "authoring"],
            assets=["gateway_rca_cost"],
            relevant_skills=["analyze_signature", "find_boundaries"],
            spec_id="gateway_rca_cost",
        ),
        TestAuthoringCase(
            id="authoring_run_step_errors",
            query="find error paths",
            query_terms=["find", "error", "paths", "test", "authoring"],
            assets=["gateway_investigate_run_step"],
            relevant_skills=["find_error_paths"],
            spec_id="gateway_investigate_run_step",
        ),
        TestAuthoringCase(
            id="authoring_execute_repeat",
            query="check idempotency and recall similar specs",
            query_terms=["check", "idempotency", "recall", "similar", "specs"],
            assets=["gateway_remediation_execute"],
            relevant_skills=["check_idempotency", "recall_similar_specs"],
            spec_id="gateway_remediation_execute",
        ),
        TestAuthoringCase(
            id="authoring_preflight_full",
            query="analyze signature, find boundaries, find error paths, then check idempotency",
            query_terms=["analyze", "signature", "find", "boundaries", "error", "paths", "check", "idempotency"],
            assets=["gateway_remediation_preflight"],
            relevant_skills=[
                "analyze_signature",
                "find_boundaries",
                "find_error_paths",
                "check_idempotency",
            ],
            spec_id="gateway_remediation_preflight",
        ),
        TestAuthoringCase(
            id="authoring_start_recall",
            query="recall similar specs, then find boundaries",
            query_terms=["recall", "similar", "specs", "find", "boundaries"],
            assets=["gateway_investigate_start"],
            relevant_skills=["recall_similar_specs", "find_boundaries"],
            spec_id="gateway_investigate_start",
        ),
    ]


def load_specs(path: str | Path | None = None) -> list[SpecUnderTest]:
    """Every spec in the fixture library, in fixture order."""
    return build_spec_library(path).specs()


def build_spec_library(path: str | Path | None = None) -> SpecLibrary:
    """Load the spec library and bind every seed case to its spec."""
    library = SpecLibrary.from_path(Path(path) if path is not None else SPECS_PATH)
    for case in load_test_authoring_seed_cases():
        library.bind(case.id, case.spec_id)
    return library


def build_test_authoring_orchestrator(ledger_path: str | Path) -> SingleAgentRCAOrchestrator:
    """The same kernel as the other domains, carrying the test-authoring skills.

    Memory, context compilation and the RCA verifier stay off: this domain reads
    a spec and reasons about it, so the only kernel machinery it needs is skill
    routing, chain execution and contract verification.
    """
    registry = SkillRegistry()
    library = build_spec_library()
    register_test_authoring_skills(registry, library)
    orchestrator = SingleAgentRCAOrchestrator(
        memory=TieredMemoryStore(enabled=False),
        context_compiler=ContextCompiler(enabled=False),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=True, top_k=4),
        verifier=Verifier(enabled=False),
        diagnosis_builder=_noop_diagnosis,
        ledger_path=ledger_path,
    )
    orchestrator.system_adapter = library
    orchestrator.contract_verifier = ContractVerifier()
    return orchestrator


def build_test_authoring_intent_router(
    orchestrator: SingleAgentRCAOrchestrator,
    *,
    deep_agent: Any | None = None,
    induction_store: str | Path | None = None,
) -> CascadingIntentRouter:
    """Cascading intent router over the test-authoring skill library.

    Authoring requests decompose deterministically onto read-only analysis
    skills, so no deep agent is wired by default (pass one to enable the
    escalation tier); the seed cases are the golden replay set that guards skill
    promotion against routing regressions.
    """
    return CascadingIntentRouter(
        orchestrator.skills,
        orchestrator.skill_controller,
        orchestrator.ledger,
        deep_agent=deep_agent,
        golden_cases=authoring_golden_cases(),
        induction_store=Path(induction_store) if induction_store is not None else None,
    )


def authoring_golden_cases() -> list[dict]:
    """Golden replay set for the promotion review: current routes must survive."""
    return [
        {
            "golden": True,
            "request": case.query,
            "query_terms": [term.lower() for term in case.query_terms],
            "expected_skill": case.relevant_skills[0],
        }
        for case in load_test_authoring_seed_cases()
    ]


def _noop_diagnosis(**kwargs: Any) -> None:
    raise NotImplementedError("test_authoring uses skill-chain analysis, not RCA diagnosis")
