from __future__ import annotations

from pathlib import Path

from core.context.compiler import ContextCompiler
from core.memory.store import TieredMemoryStore
from core.orchestrator.orchestrator import SingleAgentRCAOrchestrator
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.verifier.contracts import ContractVerifier
from core.verifier.verifier import Verifier
from domains.enterprise_ops.adapters.mock_system import MockEnterpriseSystem
from domains.enterprise_ops.schema import EnterpriseOpsCase
from domains.enterprise_ops.skills.ops_skills import register_enterprise_ops_skills


ROOT = Path(__file__).resolve().parent


def load_enterprise_seed_cases() -> list[EnterpriseOpsCase]:
    return [
        EnterpriseOpsCase(
            id="ops_quote_then_approval",
            query="按新策略报价，通过就提交审批",
            query_terms=["报价", "新策略", "审批", "提交"],
            assets=["order-1001"],
            relevant_skills=["pricing_apply_policy", "approval_submit"],
        ),
        EnterpriseOpsCase(
            id="ops_status_only",
            query="查询审批状态",
            query_terms=["查询", "审批", "状态"],
            assets=["order-1002"],
            relevant_skills=["status_check"],
        ),
        EnterpriseOpsCase(
            id="ops_bad_price",
            query="按新策略报价",
            query_terms=["报价", "新策略"],
            assets=["order-1003"],
            relevant_skills=["pricing_apply_policy"],
        ),
    ]


def build_enterprise_ops_orchestrator(ledger_path: str | Path) -> SingleAgentRCAOrchestrator:
    registry = SkillRegistry()
    adapter = MockEnterpriseSystem.from_path(ROOT / "fixtures" / "mock_system_state.json")
    register_enterprise_ops_skills(registry, adapter)
    orchestrator = SingleAgentRCAOrchestrator(
        memory=TieredMemoryStore(enabled=False),
        context_compiler=ContextCompiler(enabled=False),
        skills=registry,
        skill_controller=SkillAttentionController(enabled=True, top_k=4),
        verifier=Verifier(enabled=False),
        diagnosis_builder=_noop_diagnosis,
        ledger_path=ledger_path,
    )
    orchestrator.system_adapter = adapter
    orchestrator.contract_verifier = ContractVerifier()
    return orchestrator


def _noop_diagnosis(**kwargs):
    raise NotImplementedError("enterprise_ops uses process-chain execution, not RCA diagnosis")
