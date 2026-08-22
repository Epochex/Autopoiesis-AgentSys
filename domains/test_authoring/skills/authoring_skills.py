"""Read-only analysis skills over a spec under test.

Every skill here reads the spec and returns evidence; none of them writes
anything, and the attached contracts enforce that by comparing the observed
state before and after each call. That is the whole verticality story for this
domain: no weights were touched, the specialisation is a handful of registered
skills, a retrieval pass over past specs, and contracts the kernel already
knows how to check.

The analysis functions are plain functions first and skills second, so the
generator can call them directly when it is not running under the orchestrator.
"""

from __future__ import annotations

import re
from typing import Any

from core.skills.registry import SkillRegistry
from core.skills.spec import SkillResult, SkillSpec
from core.verifier.contracts import SkillContract, attach_contract
from domains.test_authoring.schema import SpecUnderTest


EVIDENCE_SIGNATURE = "signature_analysis"
EVIDENCE_BOUNDARIES = "boundary_analysis"
EVIDENCE_ERRORS = "error_paths"
EVIDENCE_IDEMPOTENCY = "idempotency_analysis"
EVIDENCE_SIMILAR = "similar_specs"

SKILL_NAMES: tuple[str, ...] = (
    "analyze_signature",
    "find_boundaries",
    "find_error_paths",
    "check_idempotency",
    "recall_similar_specs",
)


def analyze_signature(spec: SpecUnderTest) -> dict[str, Any]:
    """Extract the parameter list: name, type, requiredness, default, constraints."""
    return {
        "spec_id": spec.id,
        "method": spec.method,
        "path": spec.path,
        "params": [
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "default": param.default,
                "constrained": param.constrained,
            }
            for param in spec.params
        ],
        "required_params": [param.name for param in spec.required_params()],
        "optional_params": [param.name for param in spec.params if not param.required],
    }


def find_boundaries(spec: SpecUnderTest) -> dict[str, Any]:
    """Map every constrained parameter to the concrete value of each boundary role.

    Only reachable roles appear: a two-member enum has no interior, so it has no
    `just_inside`, and reporting one would invent a test nobody can write.
    """
    boundaries = {
        param.name: param.boundary_roles()
        for param in spec.params
        if param.constrained
    }
    return {
        "spec_id": spec.id,
        "boundaries": boundaries,
        "unconstrained_params": [param.name for param in spec.params if not param.constrained],
    }


def find_error_paths(spec: SpecUnderTest) -> list[dict[str, Any]]:
    """The documented failures, each with the inputs that provoke it."""
    return [
        {"code": item.code, "condition": item.condition, "trigger": dict(item.trigger)}
        for item in spec.errors
    ]


def check_idempotency(spec: SpecUnderTest) -> dict[str, Any]:
    """Whether the spec declares the operation safe to repeat.

    A spec that says nothing gets `declared=False`, which is not the same as
    "not idempotent" — the honest answer is that repetition is untested, and
    the coverage scorer treats the axis as inapplicable rather than failed.
    """
    return {
        "spec_id": spec.id,
        "declared": spec.idempotent is not None,
        "idempotent": spec.idempotent,
        "note": spec.idempotency_note,
    }


def recall_similar_specs(
    spec: SpecUnderTest,
    corpus: list[SpecUnderTest],
    *,
    top_k: int = 2,
) -> list[dict[str, Any]]:
    """Rank past specs by shared vocabulary and return what their suites learned.

    Lexical overlap over path segments, parameter names and types — deliberately
    simple, because the value here is the *retrieval*, not the ranker. Recalled
    lessons are advisory: they travel with the spec id that produced them and
    are never allowed to rewrite an assertion, so a stale lesson cannot silently
    corrupt a suite.
    """
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    mine = _tokens(spec)
    scored: list[tuple[float, SpecUnderTest]] = []
    for other in corpus:
        if other.id == spec.id:
            continue
        theirs = _tokens(other)
        union = mine | theirs
        if not union:
            continue
        overlap = len(mine & theirs) / len(union)
        if overlap > 0.0:
            scored.append((overlap, other))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [
        {
            "spec_id": other.id,
            "score": round(score, 4),
            "shared_params": sorted(
                {param.name for param in spec.params} & {param.name for param in other.params}
            ),
            "lessons": list(other.lessons),
        }
        for score, other in scored[:top_k]
    ]


def register_test_authoring_skills(registry: SkillRegistry, library) -> None:
    """Register the five read-only analysis skills against a spec library adapter."""
    specs = [
        SkillSpec(
            name="analyze_signature",
            description="Analyze a spec signature into typed parameters",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=0.5,
            tags=["test", "authoring", "analyze", "signature", "param", "parameter", "type"],
        ),
        SkillSpec(
            name="find_boundaries",
            description="Find boundary values for every constrained parameter",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=0.75,
            tags=["test", "authoring", "find", "boundary", "boundaries", "range", "limit", "edge"],
        ),
        SkillSpec(
            name="find_error_paths",
            description="Find documented error paths and how to provoke them",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=0.75,
            tags=["test", "authoring", "find", "error", "errors", "paths", "failure", "code"],
        ),
        SkillSpec(
            name="check_idempotency",
            description="Check whether the spec declares the operation safe to repeat",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=0.5,
            tags=["test", "authoring", "check", "idempotency", "idempotent", "repeat", "retry"],
        ),
        SkillSpec(
            name="recall_similar_specs",
            description="Recall similar past specs and the lessons their suites learned",
            input_schema={"case_id": "str"},
            risk="read_only",
            cost=1.0,
            tags=["test", "authoring", "recall", "similar", "specs", "past", "history", "memory"],
        ),
    ]
    for spec in specs:
        registry.register(spec, _handler(library, spec.name))
    _attach_contracts()


def _handler(library, skill_name: str):
    def run(case, state: dict[str, Any] | None = None) -> SkillResult:
        spec = library.spec_for(case.id)
        if skill_name == "analyze_signature":
            payload: dict[str, Any] = {"kind": EVIDENCE_SIGNATURE, **analyze_signature(spec)}
        elif skill_name == "find_boundaries":
            payload = {"kind": EVIDENCE_BOUNDARIES, **find_boundaries(spec)}
        elif skill_name == "find_error_paths":
            payload = {"kind": EVIDENCE_ERRORS, "spec_id": spec.id, "errors": find_error_paths(spec)}
        elif skill_name == "check_idempotency":
            payload = {"kind": EVIDENCE_IDEMPOTENCY, **check_idempotency(spec)}
        elif skill_name == "recall_similar_specs":
            payload = {
                "kind": EVIDENCE_SIMILAR,
                "spec_id": spec.id,
                "matches": recall_similar_specs(spec, library.corpus_for(spec.id)),
            }
        else:
            raise ValueError(f"unknown test authoring skill: {skill_name}")
        return SkillResult(
            skill_name=skill_name,
            evidence=[{"evidence_id": f"ev-{case.id}-{skill_name}", **payload}],
            readonly=True,
            cost=0.5,
        )

    return run


def _tokens(spec: SpecUnderTest) -> set[str]:
    text = " ".join(
        [
            spec.name,
            spec.path or "",
            spec.method or "",
            *[param.name for param in spec.params],
            *[param.type for param in spec.params],
        ]
    )
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text.replace("_", " ").replace("/", " "))}


def _attach_contracts() -> None:
    contract = SkillContract(
        preconditions=_analysis_preconditions,
        postconditions=_analysis_postconditions,
        invariants=_spec_invariants,
        write_like=False,
    )
    for name in SKILL_NAMES:
        attach_contract(name, contract)


def _analysis_preconditions(state: dict[str, Any], args: dict[str, Any]) -> list[str]:
    if "params" not in state:
        return ["analysis precondition failed: no spec is bound to this case"]
    return []


def _analysis_postconditions(before: dict[str, Any], after: dict[str, Any], result: Any) -> list[str]:
    # The whole domain is read-only analysis. If a snapshot moved, either a skill
    # wrote to the library or the library is handing out shared mutable state —
    # both make every later piece of evidence untrustworthy.
    if before != after:
        return ["analysis postcondition failed: read-only analysis changed the spec"]
    return []


def _spec_invariants(state: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for param in state.get("params") or []:
        name = param.get("name", "?")
        low, high = param.get("minimum"), param.get("maximum")
        if low is not None and high is not None and low > high:
            violations.append(f"invariant failed: {name} declares minimum above maximum")
        low_len, high_len = param.get("min_length"), param.get("max_length")
        if low_len is not None and high_len is not None and low_len > high_len:
            violations.append(f"invariant failed: {name} declares min_length above max_length")
        if param.get("type") == "enum" and not param.get("enum_values"):
            violations.append(f"invariant failed: {name} is an enum with no members")
    return violations
