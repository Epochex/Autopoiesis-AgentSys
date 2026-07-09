from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec


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


def capture_unmatched(request: str, trace_events: list[Any]) -> dict:
    case = {
        "request": request,
        "trace_events": [_dump_event(event) for event in trace_events],
        "query_terms": sorted(_tokens(request)),
    }
    INDUCTION_STORE.parent.mkdir(parents=True, exist_ok=True)
    with INDUCTION_STORE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(case, sort_keys=True, ensure_ascii=False) + "\n")
    return case


def induce_skill(unmatched_case: dict, existing_specs: list[SkillSpec]) -> SkillSpec:
    request = str(unmatched_case.get("request", ""))
    terms = _tokens(request) | set(unmatched_case.get("query_terms", []))
    domain = _best_label(terms, _DOMAIN_HINTS, "general")
    action = _best_label(terms, _ACTION_HINTS, "handle")
    base_name = _safe_name(f"{domain}_{action}")
    existing_names = {spec.name for spec in existing_specs}
    existing_tagsets = [{tag.lower() for tag in spec.tags} for spec in existing_specs]

    tags = sorted(terms | {domain, action})
    name = base_name
    if name in existing_names or any(_jaccard(set(tags), tagset) >= 0.85 for tagset in existing_tagsets):
        suffix = 2
        while f"{base_name}_{suffix}" in existing_names:
            suffix += 1
        name = f"{base_name}_{suffix}"

    risk = "approval_required" if terms & _RISKY_TERMS else "read_only"
    if action in {"pricing", "reminder"} and "submit" not in terms:
        risk = "write"

    return SkillSpec(
        name=name,
        description=f"Induced skill for {domain} {action}: {request[:120]}",
        input_schema={"request": "str"},
        risk=risk,
        cost=round(1.0 + min(len(tags), 10) * 0.1, 2),
        tags=tags,
    )


def promote_skill(candidate: SkillSpec, registry: SkillRegistry, replay_cases: list[dict]) -> bool:
    if _is_duplicate(candidate, [skill.spec for skill in registry.all()]):
        return False
    if not candidate.tags or not candidate.description.strip():
        return False

    captured = [case for case in replay_cases if not case.get("golden")]
    golden = [case for case in replay_cases if case.get("golden")]
    if not captured:
        return False

    before_hits = sum(_case_handled(case, [skill.spec for skill in registry.all()]) for case in captured)
    after_hits = sum(_case_handled(case, [* [skill.spec for skill in registry.all()], candidate]) for case in captured)
    positive_delta = after_hits > before_hits and after_hits == len(captured)

    no_regression = True
    for case in golden:
        expected = case.get("expected_skill")
        if not expected:
            continue
        before = _best_spec_name(case, [skill.spec for skill in registry.all()])
        after = _best_spec_name(case, [* [skill.spec for skill in registry.all()], candidate])
        if before == expected and after != expected:
            no_regression = False
            break

    if not positive_delta or not no_regression:
        return False

    registry.register(candidate, _induced_handler(candidate))
    return True


def _induced_handler(spec: SkillSpec):
    def run(**kwargs) -> SkillResult:
        request = str(kwargs.get("request") or getattr(kwargs.get("case"), "query", ""))
        return SkillResult(
            skill_name=spec.name,
            evidence=[
                {
                    "evidence_id": f"ev-{spec.name}",
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
    scored = []
    for spec in specs:
        tags = {tag.lower() for tag in spec.tags}
        hits = len(terms & tags)
        if hits:
            scored.append((hits, -spec.cost, spec.name))
    return sorted(scored, reverse=True)[0][2] if scored else None


def _is_duplicate(candidate: SkillSpec, existing_specs: list[SkillSpec]) -> bool:
    cand_tags = {tag.lower() for tag in candidate.tags}
    for spec in existing_specs:
        if candidate.name == spec.name:
            return True
        if _jaccard(cand_tags, {tag.lower() for tag in spec.tags}) >= 0.8:
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
