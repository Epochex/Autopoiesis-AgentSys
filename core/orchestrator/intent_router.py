from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from core.orchestrator.adaptive import has_high_blast_radius
from core.orchestrator.planner import plan_skill_chain_detailed
from core.skills import induction
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.skills.spec import RegisteredSkill
from core.trace.events import TraceEvent


RoutingTier = Literal["rule_fast_path", "library_recall", "deep_agent", "skill_induction", "unresolved"]

# Linguistic ambiguity markers: a diagnostic request ("why …", "排查 …") needs
# evidence gathering + critique, not a canned chain. These classify the request
# SHAPE — tier gates never match on specific request strings.
_DIAGNOSTIC_MARKERS = ("?", "？", "why", "为什么", "为何", "怎么", "排查", "diagnose", "root cause")


class RoutedCase(Protocol):
    """Structural contract for routable cases (domain case schemas satisfy it)."""

    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]


class RequestFeatures(BaseModel):
    """Deterministic routing features; every tier gate reads these, nothing else."""

    subgoals: list[str] = Field(default_factory=list)
    matched_subgoals: int = 0
    coverage: float = 0.0
    compound: bool = False
    diagnostic: bool = False
    high_impact: bool = False
    library_relevance: bool = False


class RoutingOutcome(BaseModel):
    """Where the cascade resolved one request and what to run next.

    `chain` is set for skill-chain tiers, `handler` for the deep-agent tier;
    `induced=True` means the request minted a new skill on this route.
    """

    tier: RoutingTier
    resolved: bool
    induced: bool = False
    chain: list[str] = Field(default_factory=list)
    handler: Any = None
    attempted_tiers: list[RoutingTier] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class CascadingIntentRouter:
    """Route one request through the four intent tiers, in order, tracing each attempt.

    1. rule fast path — `plan_skill_chain` fully decomposes a deterministic,
       non-diagnostic, non-high-impact request onto library skills;
    2. library recall — `SkillAttentionController` recalls a relevant read-only
       skill (gated on tag overlap) for a simple request the rule parser missed;
    3. deep agent — compound / diagnostic / high-impact / partially-covered
       requests are handed to the planner-executor-critic topology;
    4. self-expand on miss — a request NOTHING in the library speaks to is
       captured (`capture_unmatched`), a candidate skill is induced from the
       request + failure trace, replay-review gates promotion, and the request
       is re-routed through the expanded registry.

    Every tier attempt emits an `intent_tier_attempted` trace event and the
    winner an `intent_routed` event, so the cascade is replayable from the ledger.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        controller: SkillAttentionController,
        ledger: Any,
        *,
        deep_agent: Any | None = None,
        golden_cases: list[dict] | None = None,
        rule_min_term_hits: int = 2,
        recall_min_tag_hits: int = 1,
        enable_induction: bool = True,
        induction_store: Path | None = None,
    ):
        if rule_min_term_hits < 1:
            raise ValueError(f"rule_min_term_hits must be >= 1, got {rule_min_term_hits}")
        if recall_min_tag_hits < 1:
            raise ValueError(f"recall_min_tag_hits must be >= 1, got {recall_min_tag_hits}")
        if not hasattr(ledger, "append"):
            raise TypeError(f"ledger must expose append(TraceEvent), got {type(ledger).__name__}")
        self.registry = registry
        self.controller = controller
        self.ledger = ledger
        self.deep_agent = deep_agent
        self.golden_cases = list(golden_cases or [])
        self.rule_min_term_hits = rule_min_term_hits
        self.recall_min_tag_hits = recall_min_tag_hits
        self.enable_induction = enable_induction
        self.induction_store = induction_store

    def route(self, case: RoutedCase) -> RoutingOutcome:
        """Resolve `case` to a tier + chain/handler; raises ValueError on an empty query."""
        query = str(getattr(case, "query", "") or "")
        if not query.strip():
            raise ValueError(f"cannot route case {getattr(case, 'id', '?')!r}: empty query")
        run_id = str(uuid4())
        attempts: list[TraceEvent] = []
        outcome = self._cascade(case, run_id, attempts, allow_induction=self.enable_induction)
        self._record(
            run_id,
            case.id,
            "intent_routed",
            {
                "tier": outcome.tier,
                "resolved": outcome.resolved,
                "induced": outcome.induced,
                "chain": outcome.chain,
                "attempted_tiers": outcome.attempted_tiers,
                "features": outcome.features,
            },
            attempts,
        )
        return outcome

    def _cascade(
        self,
        case: RoutedCase,
        run_id: str,
        attempts: list[TraceEvent],
        allow_induction: bool,
    ) -> RoutingOutcome:
        plan = plan_skill_chain_detailed(case.query, self.registry)
        candidates = self.controller.select(
            self.registry.all(), list(case.query_terms), list(case.relevant_skills)
        )
        gated = [skill for skill in candidates if self._tag_hits(skill, case.query_terms) >= self.recall_min_tag_hits]
        features = RequestFeatures(
            subgoals=plan.subgoals,
            matched_subgoals=plan.matched_subgoals,
            coverage=plan.coverage,
            compound=len(plan.subgoals) > 1,
            diagnostic=self._is_diagnostic(case.query),
            high_impact=has_high_blast_radius(case),
            # relevance is judged on MATCH STRENGTH, not raw controller output or
            # an incidental 1-token overlap: the controller returns ranked skills
            # even for an unrelated query, and the rule parser will fall onto a
            # skill that merely shares a common word ("plan", "for"). A genuine
            # miss must clear the same term-hit bar as a rule hit, or induction
            # never fires.
            library_relevance=(bool(plan.chain) and plan.max_match_hits >= self.rule_min_term_hits)
            or bool(gated),
        )
        attempted: list[RoutingTier] = []

        # tier 1 — rule fast path: high-frequency deterministic requests fully
        # decomposed onto library skills; diagnostic/high-impact shapes skip it.
        rule_eligible = not features.high_impact and not features.diagnostic
        # every matched subgoal must clear the term-hit bar (weakest link), so an
        # incidental single-word overlap can't masquerade as a deterministic hit.
        rule_hit = rule_eligible and plan.full_coverage and plan.min_match_hits >= self.rule_min_term_hits
        attempted.append("rule_fast_path")
        self._record(
            run_id,
            case.id,
            "intent_tier_attempted",
            {
                "tier": "rule_fast_path",
                "eligible": rule_eligible,
                "hit": rule_hit,
                "chain": plan.chain,
                "coverage": round(features.coverage, 4),
                "unmatched_subgoals": plan.unmatched_subgoals,
            },
            attempts,
        )
        if rule_hit:
            return self._outcome("rule_fast_path", attempted, features, chain=plan.chain)

        # tier 2 — library recall: simple request the rule parser could not
        # decompose, but the attention controller recalls a relevant read-only skill.
        recall_eligible = not features.compound and not features.high_impact and not features.diagnostic
        recall_hit = recall_eligible and bool(gated)
        attempted.append("library_recall")
        self._record(
            run_id,
            case.id,
            "intent_tier_attempted",
            {
                "tier": "library_recall",
                "eligible": recall_eligible,
                "hit": recall_hit,
                "candidates": [skill.spec.name for skill in gated],
                "min_tag_hits": self.recall_min_tag_hits,
            },
            attempts,
        )
        if recall_hit:
            return self._outcome("library_recall", attempted, features, chain=[gated[0].spec.name])

        # tier 3 — deep agent: compound/diagnostic/high-impact/partially-covered
        # requests with SOME library relevance escalate to planner-executor-critic.
        reasons = [
            name
            for name, flag in (
                ("compound", features.compound),
                ("diagnostic", features.diagnostic),
                ("high_impact", features.high_impact),
                ("partial_coverage", 0.0 < features.coverage < 1.0),
            )
            if flag
        ]
        deep_hit = bool(reasons) and features.library_relevance and self.deep_agent is not None
        attempted.append("deep_agent")
        self._record(
            run_id,
            case.id,
            "intent_tier_attempted",
            {
                "tier": "deep_agent",
                "eligible": deep_hit,
                "hit": deep_hit,
                "reasons": reasons,
                "deep_agent_available": self.deep_agent is not None,
            },
            attempts,
        )
        if deep_hit:
            return self._outcome("deep_agent", attempted, features, handler=self.deep_agent)

        # tier 4 — self-expand on miss: nothing in the library speaks to the
        # request (zero relevance), so capture it and try to induce a skill.
        if allow_induction and not features.library_relevance:
            attempted.append("skill_induction")
            return self._self_expand(case, run_id, attempts, attempted, features)

        return RoutingOutcome(
            tier="unresolved",
            resolved=False,
            attempted_tiers=attempted,
            features=features.model_dump(),
        )

    def _self_expand(
        self,
        case: RoutedCase,
        run_id: str,
        attempts: list[TraceEvent],
        attempted: list[RoutingTier],
        features: RequestFeatures,
    ) -> RoutingOutcome:
        """Miss → capture → induce → replay-review → re-route through the expanded library."""
        captured = induction.capture_unmatched(case.query, attempts, store=self.induction_store)
        self._record(
            run_id,
            case.id,
            "unmatched_captured",
            {"request": case.query, "query_terms": captured["query_terms"], "trace_events": len(captured["trace_events"])},
            attempts,
        )
        candidate = induction.induce_skill(captured, [skill.spec for skill in self.registry.all()])
        self._record(
            run_id,
            case.id,
            "skill_induced",
            {"name": candidate.name, "tags": candidate.tags, "risk": candidate.risk},
            attempts,
        )
        promoted = induction.promote_skill(candidate, self.registry, [captured, *self.golden_cases])
        self._record(run_id, case.id, "skill_promoted", {"name": candidate.name, "promoted": promoted}, attempts)
        if not promoted:
            return RoutingOutcome(
                tier="unresolved",
                resolved=False,
                attempted_tiers=attempted,
                features=features.model_dump(),
            )

        rerouted = self._cascade(case, run_id, attempts, allow_induction=False)
        if rerouted.resolved:
            chain, handler = rerouted.chain, rerouted.handler
        else:
            # The review gate already proved the candidate routes AND executes the
            # originally-failed request; fall back to it directly for compound shapes.
            chain, handler = [candidate.name], None
        return RoutingOutcome(
            tier="skill_induction",
            resolved=True,
            induced=True,
            chain=chain,
            handler=handler,
            attempted_tiers=[*attempted, *rerouted.attempted_tiers],
            features=features.model_dump(),
        )

    def _outcome(
        self,
        tier: RoutingTier,
        attempted: list[RoutingTier],
        features: RequestFeatures,
        *,
        chain: list[str] | None = None,
        handler: Any = None,
    ) -> RoutingOutcome:
        return RoutingOutcome(
            tier=tier,
            resolved=True,
            chain=list(chain or []),
            handler=handler,
            attempted_tiers=attempted,
            features=features.model_dump(),
        )

    @staticmethod
    def _tag_hits(skill: RegisteredSkill, query_terms: list[str]) -> int:
        terms = {term.lower() for term in query_terms}
        return len(terms & {tag.lower() for tag in skill.spec.tags})

    @staticmethod
    def _is_diagnostic(query: str) -> bool:
        lowered = query.lower()
        return any(marker in lowered for marker in _DIAGNOSTIC_MARKERS)

    def _record(self, run_id: str, case_id: str, kind: str, payload: dict, attempts: list[TraceEvent]) -> None:
        event = TraceEvent(run_id=run_id, case_id=str(case_id), kind=kind, payload=payload)
        self.ledger.append(event)
        attempts.append(event)
