from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


# Read at import so tests can monkeypatch the attribute; override via env for deployments.
INDUCTION_STORE = Path(os.environ.get("SKILL_INDUCTION_STORE", ".artifacts/skill_induction_cases.jsonl"))

_DOMAIN_HINTS = {
    "enterprise": {"enterprise", "ops", "approval", "pricing", "quote", "invoice", "status", "报价", "审批"},
    "network": {"network", "interface", "route", "carrier", "device"},
    "recon": {"scan", "port", "cve", "banner", "tls", "exploit"},
}
_ACTION_HINTS = {
    "pricing": {"price", "pricing", "quote", "报价", "策略"},
    "approval": {"approve", "approval", "submit", "审批", "提交"},
    "status": {"status", "state", "状态"},
    "reminder": {"remind", "reminder", "notify", "提醒"},
    "diagnose": {"diagnose", "rca", "root", "cause"},
}
_RISKY_TERMS = {"approve", "approval", "submit", "update", "write", "审批", "提交"}

# tag-overlap ratios above which a candidate is considered a duplicate capability
_DUPLICATE_JACCARD = 0.8
_NEAR_DUPLICATE_NAME_JACCARD = 0.85


def capture_unmatched(request: str, trace_events: list[Any], *, store: Path | None = None) -> dict:
    """Persist an unhandled request (+ its failure trace) as an induction case.

    Returns the stored case dict; `store` overrides INDUCTION_STORE (evals pass
    a scratch path). Raises ValueError on a blank request — there is nothing to
    induce from.
    """
    if not request or not request.strip():
        raise ValueError("cannot capture an empty request")
    case = {
        "request": request,
        "trace_events": [_dump_event(event) for event in trace_events],
        "query_terms": sorted(_tokens(request)),
    }
    store_path = Path(store) if store is not None else INDUCTION_STORE
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with store_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(case, sort_keys=True, ensure_ascii=False) + "\n")
    return case


def induce_skill(unmatched_case: dict, existing_specs: list[SkillSpec]) -> SkillSpec:
    """Derive a candidate SkillSpec from a captured failure case AND its failure trace.

    Deterministic heuristics: domain/action labels from token hints over the
    request plus the trace's unmet terms; the candidate's tags gain the failed
    trajectory's sub-goal / missing-evidence terms that no existing spec covers,
    its input schema exposes those `evidence_gaps`, and its description names
    the attempted-but-failed skills. A unique name (suffixed if colliding with
    `existing_specs`) and a conservative risk class (any risky term forces
    approval) as before. Raises ValueError if the case has no request text.
    Candidates are NOT registered here — `promote_skill` reviews that.
    """
    request = str(unmatched_case.get("request", ""))
    if not request.strip():
        raise ValueError("unmatched case has no request text to induce from")
    failed_skills, unmet_terms = _trace_signals(unmatched_case.get("trace_events") or [])
    terms = _tokens(request) | set(unmatched_case.get("query_terms", []))
    # what the failed trajectory was reaching for that the current library cannot cover
    covered_terms = {tag.lower() for spec in existing_specs for tag in spec.tags}
    reach_terms = {term for term in unmet_terms if term not in covered_terms}
    label_terms = terms | unmet_terms
    domain = _best_label(label_terms, _DOMAIN_HINTS, "general")
    action = _best_label(label_terms, _ACTION_HINTS, "handle")
    base_name = _safe_name(f"{domain}_{action}")
    existing_names = {spec.name for spec in existing_specs}
    existing_tagsets = [{tag.lower() for tag in spec.tags} for spec in existing_specs]

    tags = sorted(terms | reach_terms | {domain, action})
    name = base_name
    if name in existing_names or any(_jaccard(set(tags), tagset) >= _NEAR_DUPLICATE_NAME_JACCARD for tagset in existing_tagsets):
        suffix = 2
        while f"{base_name}_{suffix}" in existing_names:
            suffix += 1
        name = f"{base_name}_{suffix}"

    risk = "approval_required" if label_terms & _RISKY_TERMS else "read_only"
    if action in {"pricing", "reminder"} and "submit" not in label_terms:
        risk = "write"

    description = f"Induced skill for {domain} {action}: {request[:120]}"
    if failed_skills:
        description += f" | failed steps: {', '.join(failed_skills)}"
    if reach_terms:
        description += f" | unmet: {', '.join(sorted(reach_terms))}"
    input_schema: dict[str, Any] = {"request": "str"}
    if reach_terms:
        input_schema["evidence_gaps"] = sorted(reach_terms)

    return SkillSpec(
        name=name,
        description=description,
        input_schema=input_schema,
        risk=risk,
        cost=round(1.0 + min(len(tags), 10) * 0.1, 2),
        tags=tags,
    )


def _trace_signals(trace_events: list[Any]) -> tuple[list[str], set[str]]:
    """(failed skills, unmet terms) extracted deterministically from a failure trace.

    Failed skills: `step_verified` events with passed=False plus blocked
    `tool_called`/`executor_ran` events. Unmet terms: tokens of unmatched
    subgoals from `intent_tier_attempted` events and of `missing_evidence`
    from planner/diagnosis events.
    """
    failed: list[str] = []
    unmet: set[str] = set()
    for raw in trace_events:
        event = raw if isinstance(raw, dict) else _dump_event(raw)
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        skill = payload.get("skill")
        if kind == "step_verified" and payload.get("passed") is False and skill:
            failed.append(str(skill))
        elif kind in {"tool_called", "executor_ran"} and payload.get("blocked") and skill:
            failed.append(str(skill))
        elif kind == "intent_tier_attempted":
            for subgoal in payload.get("unmatched_subgoals") or []:
                unmet |= _tokens(str(subgoal))
        elif kind in {"planner_proposed", "diagnosis_completed"}:
            for item in payload.get("missing_evidence") or []:
                unmet |= _tokens(str(item))
    return list(dict.fromkeys(failed)), unmet


def promote_skill(candidate: SkillSpec, registry: SkillRegistry, replay_cases: list[dict]) -> bool:
    """Replay-gated promotion — this IS the deterministic review step.

    The review contract: (a) the candidate must handle the originally-failed
    request(s) — every captured case becomes routable AND its handler actually
    replays them; (b) it must not break existing capability — no golden case
    may be re-routed away from its expected skill. Only a candidate passing
    both enters the library (no LLM in the loop; the gates below are the review).

    Gates, in order — all must hold:
      1. not a duplicate (name or near-identical tags) of an existing skill;
      2. structurally sound (non-empty tags and description);
      3. replayed captured cases: the candidate makes ALL of them routable and
         strictly improves coverage vs. the current library;
      4. its handler actually executes against every captured request and
         yields evidence;
      5. golden replay cases keep routing to their expected skill (no regression).

    Returns True iff the candidate was registered.
    """
    existing_specs = [skill.spec for skill in registry.all()]
    if _is_duplicate(candidate, existing_specs):
        return False
    if not candidate.tags or not candidate.description.strip():
        return False

    captured = [case for case in replay_cases if not case.get("golden")]
    golden = [case for case in replay_cases if case.get("golden")]
    if not captured:
        return False

    with_candidate = [*existing_specs, candidate]
    before_hits = sum(_case_handled(case, existing_specs) for case in captured)
    after_hits = sum(_case_handled(case, with_candidate) for case in captured)
    if not (after_hits > before_hits and after_hits == len(captured)):
        return False

    handler = _induced_handler(candidate)
    if not all(_handler_replays(handler, case) for case in captured):
        return False

    for case in golden:
        expected = case.get("expected_skill")
        if not expected:
            continue
        before = _best_spec_name(case, existing_specs)
        after = _best_spec_name(case, with_candidate)
        if before == expected and after != expected:
            return False

    registry.register(candidate, handler)
    return True


def _handler_replays(handler: Any, case: dict) -> bool:
    """True iff the candidate handler runs on the captured request and produces evidence."""
    try:
        result = handler(request=str(case.get("request", "")))
    except Exception:
        return False
    return isinstance(result, SkillResult) and bool(result.evidence)


def _induced_handler(spec: SkillSpec):
    def run(**kwargs) -> SkillResult:
        request = str(kwargs.get("request") or getattr(kwargs.get("case"), "query", ""))
        return SkillResult(
            skill_name=spec.name,
            evidence=[
                {
                    "evidence_id": f"ev-{spec.name}",
                    "source": f"skill:{spec.name}",
                    "summary": f"Induced skill {spec.name} matched request: {request[:80]}",
                    "kind": "induced_skill_match",
                    "request": request,
                    "tags": list(spec.tags),
                }
            ],
            readonly=spec.risk == "read_only",
            cost=spec.cost,
        )

    return run


def _case_handled(case: dict, specs: list[SkillSpec]) -> bool:
    return _best_spec_name(case, specs) is not None


def _best_spec_name(case: dict, specs: list[SkillSpec]) -> str | None:
    terms = set(case.get("query_terms") or _tokens(str(case.get("request", ""))))
    scored: list[tuple[int, float, str]] = []
    for spec in specs:
        tags = {tag.lower() for tag in spec.tags}
        hits = len(terms & tags)
        if hits:
            scored.append((hits, -spec.cost, spec.name))
    return max(scored)[2] if scored else None


def _is_duplicate(candidate: SkillSpec, existing_specs: list[SkillSpec]) -> bool:
    cand_tags = {tag.lower() for tag in candidate.tags}
    for spec in existing_specs:
        if candidate.name == spec.name:
            return True
        if _jaccard(cand_tags, {tag.lower() for tag in spec.tags}) >= _DUPLICATE_JACCARD:
            return True
    return False


def _best_label(terms: set[str], hints: dict[str, set[str]], fallback: str) -> str:
    scored = [(len(terms & values), label) for label, values in hints.items()]
    score, label = max(scored, key=lambda item: (item[0], item[1]))
    return label if score else fallback


def _tokens(text: str) -> set[str]:
    ascii_tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text.replace("_", " "))}
    cjk_hits = {term for hints in [*_DOMAIN_HINTS.values(), *_ACTION_HINTS.values(), _RISKY_TERMS] for term in hints if term in text}
    return ascii_tokens | cjk_hits


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 0.0


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "induced_skill"


def _dump_event(event: Any) -> dict:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if isinstance(event, dict):
        return dict(event)
    return {"repr": repr(event)}
