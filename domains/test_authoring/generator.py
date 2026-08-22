"""Turning a spec plus skill evidence into a test suite.

The generator works with no model at all. Boundaries, error paths and
requiredness are already stated in the spec, so the cases that follow from them
are derivable, not inventable — and deriving them is what makes the eval free
to run and what lets this domain exist without a fine-tune.

A model is optional and additive. It is asked for the cases a schema cannot
imply (semantic combinations, ordering, realistic payloads), its output is
validated against the same case model as everything else, and it is capped. If
it returns nothing usable, the deterministic suite still stands.

Recalled knowledge from `recall_similar_specs` is advisory. It lands in the
suite's `notes` and in the rationale of the cases it bears on, attributed to
the spec it came from. It never rewrites an assertion: a lesson learned on a
neighbouring endpoint is a hint to the author, and letting it silently change
what a case expects is how stale knowledge becomes a wrong test.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from domains.test_authoring.schema import (
    ErrorPath,
    ParamSpec,
    SpecUnderTest,
    TestCase,
    TestSuite,
)
from domains.test_authoring.skills.authoring_skills import (
    EVIDENCE_BOUNDARIES,
    EVIDENCE_ERRORS,
    EVIDENCE_IDEMPOTENCY,
    EVIDENCE_SIMILAR,
    find_boundaries,
    find_error_paths,
)

# A model may only add this many cases per spec. Generation is the cheap half of
# this domain; an uncapped "write more tests" loop is how it stops being cheap.
MODEL_CASE_CEILING = 12

SUITE_SCHEMA: dict[str, Any] = {
    "cases": [
        {
            "name": "str",
            "kind": "happy_path | boundary | negative | error_path | idempotency",
            "inputs": {"param": "value"},
            "expected": {"status": "int or str"},
            "rationale": "str",
            "targets_param": "str or null",
            "boundary_role": "min | max | just_inside | just_outside | null",
            "targets_error": "str or null",
            "repeat": "int >= 1",
        }
    ]
}


def generate_suite(
    spec: SpecUnderTest,
    *,
    evidence: Sequence[dict[str, Any]] | None = None,
    model: Any | None = None,
) -> TestSuite:
    """Build a suite for `spec`, deterministically, optionally extended by a model.

    `evidence` is the output of the analysis skills; when it is absent the same
    analysis functions are called directly, so the deterministic result is
    identical whether or not the orchestrator ran. Raises ValueError if the spec
    declares no parameters and no errors — there is nothing to author against.
    """
    if not spec.params and not spec.errors:
        raise ValueError(f"spec {spec.id!r} declares neither parameters nor errors")

    index = _evidence_index(evidence or [])
    boundaries = _boundaries_from(spec, index)
    errors = _errors_from(spec, index)
    idempotent = _idempotency_from(spec, index)
    recalled = index.get(EVIDENCE_SIMILAR, {}).get("matches") or []

    cases: list[TestCase] = []
    cases.extend(_happy_cases(spec))
    cases.extend(_boundary_cases(spec, boundaries))
    cases.extend(_negative_cases(spec))
    cases.extend(_error_cases(spec, errors))
    cases.extend(_idempotency_cases(spec, idempotent))

    notes = _apply_recall(cases, recalled)
    suite = TestSuite(spec_id=spec.id, source="deterministic", cases=cases, notes=notes)

    if model is not None:
        extra = _ask_model(spec, model)
        suite = merge_suites(suite, extra)
        suite.source = "deterministic+model"
    return suite


def merge_suites(base: TestSuite, extra: list[TestCase]) -> TestSuite:
    """Append `extra` to `base`, dropping any case whose name is already taken."""
    seen = {case.name for case in base.cases}
    merged = list(base.cases)
    for case in extra:
        if case.name in seen:
            continue
        seen.add(case.name)
        merged.append(case)
    return TestSuite(spec_id=base.spec_id, source=base.source, cases=merged, notes=list(base.notes))


def _evidence_index(evidence: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Last item of each evidence kind, keyed by kind."""
    index: dict[str, dict[str, Any]] = {}
    for item in evidence:
        kind = str(item.get("kind", ""))
        if kind:
            index[kind] = item
    return index


def _boundaries_from(spec: SpecUnderTest, index: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    recorded = index.get(EVIDENCE_BOUNDARIES, {}).get("boundaries")
    if isinstance(recorded, dict) and recorded:
        return {str(name): dict(roles) for name, roles in recorded.items()}
    return find_boundaries(spec)["boundaries"]


def _errors_from(spec: SpecUnderTest, index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    recorded = index.get(EVIDENCE_ERRORS, {}).get("errors")
    if isinstance(recorded, list) and recorded:
        return [dict(item) for item in recorded]
    return find_error_paths(spec)


def _idempotency_from(spec: SpecUnderTest, index: dict[str, dict[str, Any]]) -> bool | None:
    recorded = index.get(EVIDENCE_IDEMPOTENCY)
    if recorded is not None and recorded.get("declared"):
        return bool(recorded.get("idempotent"))
    return spec.idempotent


def _happy_inputs(spec: SpecUnderTest) -> dict[str, Any]:
    return {param.name: param.typical_value() for param in spec.required_params()}


def _happy_cases(spec: SpecUnderTest) -> list[TestCase]:
    cases = [
        TestCase(
            name=f"{spec.id}::happy_minimal",
            kind="happy_path",
            inputs=_happy_inputs(spec),
            expected=dict(spec.expected_success),
            rationale="只传必填参数，确认基本调用通得过。",
        )
    ]
    optional = [param for param in spec.params if not param.required]
    if optional:
        inputs = _happy_inputs(spec)
        for param in optional:
            inputs[param.name] = _varied_value(param)
        cases.append(
            TestCase(
                name=f"{spec.id}::happy_full",
                kind="happy_path",
                inputs=inputs,
                expected=dict(spec.expected_success),
                rationale="选填参数全部给非默认值，确认它们真的被读到了。",
            )
        )
    return cases


def _boundary_cases(spec: SpecUnderTest, boundaries: dict[str, dict[str, Any]]) -> list[TestCase]:
    cases: list[TestCase] = []
    for param in spec.params:
        roles = boundaries.get(param.name) or {}
        for role, value in roles.items():
            inputs = _happy_inputs(spec)
            inputs[param.name] = value
            # Only the crossing case is expected to fail; the other three sit on
            # legal values and must still succeed, which is the half of boundary
            # testing people forget.
            expected = {"status": 422} if role == "just_outside" else dict(spec.expected_success)
            cases.append(
                TestCase(
                    name=f"{spec.id}::boundary_{param.name}_{role}",
                    kind="boundary",
                    inputs=inputs,
                    expected=expected,
                    rationale=f"{param.name} 取 {role} 位置的值，看边界判断有没有差一位。",
                    targets_param=param.name,
                    boundary_role=role,
                )
            )
    return cases


def _negative_cases(spec: SpecUnderTest) -> list[TestCase]:
    cases: list[TestCase] = []
    for param in spec.required_params():
        inputs = _happy_inputs(spec)
        inputs.pop(param.name, None)
        cases.append(
            TestCase(
                name=f"{spec.id}::negative_missing_{param.name}",
                kind="negative",
                inputs=inputs,
                expected={"status": 422},
                rationale=f"不传必填的 {param.name}，看是不是真的被拦下来。",
                targets_param=param.name,
            )
        )
    for param in spec.params:
        inputs = _happy_inputs(spec)
        inputs[param.name] = param.wrong_type_value()
        cases.append(
            TestCase(
                name=f"{spec.id}::negative_type_{param.name}",
                kind="negative",
                inputs=inputs,
                expected={"status": 422},
                rationale=f"{param.name} 传错类型，看类型校验在不在。",
                targets_param=param.name,
            )
        )
    return cases


def _error_cases(spec: SpecUnderTest, errors: list[dict[str, Any]]) -> list[TestCase]:
    cases: list[TestCase] = []
    for item in errors:
        error = ErrorPath.model_validate(item)
        inputs = _happy_inputs(spec)
        inputs.update(error.trigger)
        cases.append(
            TestCase(
                name=f"{spec.id}::error_{error.code}",
                kind="error_path",
                inputs=inputs,
                expected={"status": error.code},
                rationale=f"触发文档写明的失败：{error.condition}",
                targets_error=error.code,
            )
        )
    return cases


def _idempotency_cases(spec: SpecUnderTest, idempotent: bool | None) -> list[TestCase]:
    if idempotent is None:
        return []
    outcome = "second_call_matches_first" if idempotent else "second_call_conflicts"
    return [
        TestCase(
            name=f"{spec.id}::idempotency_repeat",
            kind="idempotency",
            inputs=_happy_inputs(spec),
            expected={"status": spec.expected_success.get("status", 200), "repeat": outcome},
            rationale=spec.idempotency_note or "同样的调用连做两次，看第二次的结果符不符合声明。",
            repeat=2,
        )
    ]


def _apply_recall(cases: list[TestCase], recalled: list[dict[str, Any]]) -> list[str]:
    """Attach recalled lessons to the cases they bear on; return the suite notes."""
    notes: list[str] = []
    for match in recalled:
        spec_id = str(match.get("spec_id", ""))
        shared = set(match.get("shared_params") or [])
        for lesson in match.get("lessons") or []:
            notes.append(f"参考 {spec_id}：{lesson}")
            for case in cases:
                if case.targets_param and case.targets_param in shared:
                    case.rationale = f"{case.rationale}（参考 {spec_id}：{lesson}）"
    return notes


def _varied_value(param: ParamSpec) -> Any:
    """A legal value that is not this parameter's default."""
    value = param.typical_value()
    if value != param.default:
        return value
    if param.type == "enum":
        for member in param.enum_values:
            if member != param.default:
                return member
        return value
    if param.type == "bool":
        return not bool(value)
    if param.type in {"int", "float"}:
        for candidate in (value + 1, value - 1):
            if param.satisfies(candidate):
                return candidate
        return value
    longer = f"{value}y"
    return longer if param.satisfies(longer) else value


def _ask_model(spec: SpecUnderTest, client: Any) -> list[TestCase]:
    """One capped model call for the cases a schema cannot imply.

    Malformed cases are dropped rather than raised: a model that returns six
    good cases and one broken one has still helped, and the deterministic suite
    is the floor either way.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "你是接口测试用例作者。只输出 JSON，不要解释。"
                "用例要覆盖正常路径、边界、非法输入、文档写明的错误、以及重复调用。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"待测接口：{json.dumps(spec.model_dump(), ensure_ascii=False)}\n\n"
                f"按这个结构返回：{json.dumps(SUITE_SCHEMA, ensure_ascii=False)}"
            ),
        },
    ]
    response = client.complete_json(messages, schema_name="test_authoring_suite")
    parsed: list[TestCase] = []
    for item in (response.get("cases") or [])[:MODEL_CASE_CEILING]:
        try:
            parsed.append(TestCase.model_validate(item))
        except ValidationError:
            continue
    return parsed
