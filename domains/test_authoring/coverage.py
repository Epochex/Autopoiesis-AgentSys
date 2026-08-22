"""Scoring a test suite on what it does *not* cover.

Pass rate is the wrong number to look at here. A suite of forty happy-path
calls passes forty times and tells you nothing about the guards, and an agent
graded only on pass rate learns to write exactly that suite. So this module
scores four orthogonal axes and one shape:

``kind_balance`` — how evenly the suite spreads across happy path, boundary,
negative, error path and idempotency. Reported as ``skew``: one minus the
normalised entropy over the kinds this spec can actually have. 0 is an even
spread; 1 is everything piled into one kind.

``param_coverage`` — how many declared parameters are exercised with a value
that is not simply their default. A parameter only ever passed its default is
not under test; the call would behave identically if the parameter did not
exist.

``boundary_coverage`` — for every parameter with a constraint, whether min,
max, just-inside and just-outside are all present. The roles are read off the
values, never off the case's own label, so a case that calls itself a boundary
test while passing a mid-range value earns nothing.

``error_coverage`` — how many documented failures have a case that provokes
them *and* expects the right code.

The threshold on skew is 0.35, which is roughly "you are effectively using
fewer than three of the five kinds". It is deliberately not a demand for a
uniform split: a spec with three constrained parameters legitimately needs more
boundary cases than happy-path cases. What it rules out is a suite that has
collapsed onto one or two kinds.

``self_consistency`` is reported alongside but is not an axis. It asks only
whether each case's claims match its own inputs — which is the closest thing
here to "did the tests pass". Keeping it separate is the point: a suite can
score 1.0 on it and still be badly skewed, and the report has to show both.
"""

from __future__ import annotations

from math import log
from typing import Any

from pydantic import BaseModel, Field

from domains.test_authoring.schema import ParamSpec, SpecUnderTest, TestCase, TestSuite


KINDS: tuple[str, ...] = ("happy_path", "boundary", "negative", "error_path", "idempotency")

# See the module docstring: this is a lopsidedness gate, not a uniformity demand.
SKEW_THRESHOLD = 0.35

# Reported in a fixed order so `weakest_axis` is deterministic under ties.
AXES: tuple[str, ...] = ("kind_balance", "boundary_coverage", "error_coverage", "param_coverage")


class BoundaryGap(BaseModel):
    """Which boundary positions one parameter is missing."""

    param: str
    present: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CoverageReport(BaseModel):
    """The full scorecard for one suite against one spec."""

    spec_id: str
    source: str
    total_cases: int

    kind_counts: dict[str, int]
    kind_shares: dict[str, float]
    applicable_kinds: list[str]
    missing_kinds: list[str]
    dominant_kind: str
    dominant_share: float
    skew: float
    skew_threshold: float
    skewed: bool

    param_coverage: float
    unexercised_params: list[str]
    default_only_params: list[str]

    boundary_coverage: float
    boundary_gaps: list[BoundaryGap]
    mislabelled_boundaries: list[str]

    error_coverage: float
    uncovered_errors: list[str]
    phantom_errors: list[str]

    self_consistency: float
    inconsistent_cases: list[str]

    axis_scores: dict[str, float]
    weakest_axis: str
    recommendation: str

    def is_acceptable(self, *, floor: float = 0.6) -> bool:
        """True when the suite is neither skewed nor weak on any single axis."""
        return not self.skewed and min(self.axis_scores.values()) >= floor


def applicable_kinds(spec: SpecUnderTest) -> tuple[str, ...]:
    """The kinds a suite for `spec` can be expected to contain.

    A spec with no documented errors must not be marked down for having no
    error-path cases — an unreachable axis is a permanent false gap.
    """
    kinds = ["happy_path", "negative"]
    if any(param.constrained for param in spec.params):
        kinds.append("boundary")
    if spec.errors:
        kinds.append("error_path")
    if spec.idempotent is not None:
        kinds.append("idempotency")
    return tuple(kind for kind in KINDS if kind in kinds)


def kind_skew(counts: dict[str, int], denominator: tuple[str, ...]) -> float:
    """One minus the normalised entropy of `counts` over `denominator`.

    0.0 is an even spread across every kind in the denominator, 1.0 is every
    case in one kind. An empty suite is maximally skewed, not undefined.
    """
    total = sum(counts.get(kind, 0) for kind in denominator)
    if total == 0:
        return 1.0
    if len(denominator) < 2:
        return 0.0
    entropy = 0.0
    for kind in denominator:
        share = counts.get(kind, 0) / total
        if share > 0.0:
            entropy -= share * log(share)
    return max(0.0, min(1.0, 1.0 - entropy / log(len(denominator))))


def score_suite(spec: SpecUnderTest, suite: TestSuite) -> CoverageReport:
    """Score `suite` against `spec` on every axis.

    Raises ValueError when the suite was written for a different spec — pairing
    them silently would produce a confident, meaningless scorecard.
    """
    if suite.spec_id != spec.id:
        raise ValueError(
            f"suite targets spec {suite.spec_id!r} but was scored against {spec.id!r}"
        )

    counts = {kind: len(suite.of_kind(kind)) for kind in KINDS}
    total = len(suite.cases)
    applicable = applicable_kinds(spec)
    denominator = tuple(kind for kind in KINDS if kind in applicable or counts[kind] > 0)
    shares = {kind: (counts[kind] / total if total else 0.0) for kind in denominator}
    dominant = max(denominator, key=lambda kind: (counts[kind], -KINDS.index(kind)))
    skew = kind_skew(counts, denominator)

    param_score, unexercised, default_only = _param_axis(spec, suite)
    boundary_score, gaps, mislabelled = _boundary_axis(spec, suite)
    error_score, uncovered, phantom = _error_axis(spec, suite)
    consistency, inconsistent = _self_consistency(spec, suite)

    axis_scores = {
        "kind_balance": round(1.0 - skew, 4),
        "boundary_coverage": round(boundary_score, 4),
        "error_coverage": round(error_score, 4),
        "param_coverage": round(param_score, 4),
    }
    missing = [kind for kind in applicable if counts[kind] == 0]
    weakest = _weakest_axis(axis_scores, skewed=skew > SKEW_THRESHOLD, missing_kinds=missing)

    report = CoverageReport(
        spec_id=spec.id,
        source=suite.source,
        total_cases=total,
        kind_counts={kind: counts[kind] for kind in KINDS},
        kind_shares={kind: round(share, 4) for kind, share in shares.items()},
        applicable_kinds=list(applicable),
        missing_kinds=missing,
        dominant_kind=dominant,
        dominant_share=round(shares.get(dominant, 0.0), 4),
        skew=round(skew, 4),
        skew_threshold=SKEW_THRESHOLD,
        skewed=skew > SKEW_THRESHOLD,
        param_coverage=axis_scores["param_coverage"],
        unexercised_params=unexercised,
        default_only_params=default_only,
        boundary_coverage=axis_scores["boundary_coverage"],
        boundary_gaps=gaps,
        mislabelled_boundaries=mislabelled,
        error_coverage=axis_scores["error_coverage"],
        uncovered_errors=uncovered,
        phantom_errors=phantom,
        self_consistency=round(consistency, 4),
        inconsistent_cases=inconsistent,
        axis_scores=axis_scores,
        weakest_axis=weakest,
        recommendation="",
    )
    report.recommendation = _recommend(report)
    return report


def _param_axis(spec: SpecUnderTest, suite: TestSuite) -> tuple[float, list[str], list[str]]:
    """Share of parameters exercised with something other than their default."""
    if not spec.params:
        return 1.0, [], []
    unexercised: list[str] = []
    default_only: list[str] = []
    varied = 0
    for param in spec.params:
        # Omitting a required parameter on purpose is an exercise of it: that is
        # the case the requiredness guard owns.
        touched = [case for case in suite.cases if param.name in case.inputs or case.targets_param == param.name]
        if not touched:
            unexercised.append(param.name)
            continue
        values = [case.inputs[param.name] for case in touched if param.name in case.inputs]
        omitted = any(param.name not in case.inputs for case in touched)
        if omitted or any(value != param.default for value in values):
            varied += 1
        else:
            default_only.append(param.name)
    return varied / len(spec.params), unexercised, default_only


def _boundary_axis(spec: SpecUnderTest, suite: TestSuite) -> tuple[float, list[BoundaryGap], list[str]]:
    """Per-parameter boundary-role coverage, judged on values not on labels."""
    constrained = [param for param in spec.params if param.constrained]
    mislabelled = _mislabelled_boundaries(spec, suite)
    if not constrained:
        return 1.0, [], mislabelled

    gaps: list[BoundaryGap] = []
    scores: list[float] = []
    for param in constrained:
        required = param.required_boundary_roles()
        observed = set()
        for case in suite.cases:
            if param.name not in case.inputs:
                continue
            role = param.role_of(case.inputs[param.name])
            if role is not None:
                observed.add(role)
        covered = [role for role in required if role in observed]
        scores.append(len(covered) / len(required))
        missing = [role for role in required if role not in observed]
        if missing:
            gaps.append(BoundaryGap(param=param.name, present=covered, missing=missing))
    return sum(scores) / len(scores), gaps, mislabelled


def _mislabelled_boundaries(spec: SpecUnderTest, suite: TestSuite) -> list[str]:
    """Cases whose declared boundary role is not where their value actually sits."""
    wrong: list[str] = []
    for case in suite.cases:
        if not case.boundary_role:
            continue
        param = spec.param(case.targets_param or "")
        if param is None or case.targets_param not in case.inputs:
            wrong.append(case.name)
            continue
        if param.role_of(case.inputs[case.targets_param]) != case.boundary_role:
            wrong.append(case.name)
    return wrong


def _error_axis(spec: SpecUnderTest, suite: TestSuite) -> tuple[float, list[str], list[str]]:
    """Share of documented errors with a case that both targets and expects them."""
    documented = [item.code for item in spec.errors]
    covered: set[str] = set()
    phantom: list[str] = []
    for case in suite.cases:
        code = case.targets_error
        if not code:
            continue
        if code not in documented:
            phantom.append(case.name)
            continue
        if _expects_code(case, code):
            covered.add(code)
    if not documented:
        return 1.0, [], phantom
    return len(covered) / len(documented), [code for code in documented if code not in covered], phantom


def _expects_code(case: TestCase, code: str) -> bool:
    """True when the case's expectation actually names `code`.

    Targeting an error without expecting its code is the failure mode where a
    case provokes a 422 and asserts a 200 — it runs, it is labelled, and it
    proves the opposite of what the label claims.
    """
    return code in {str(value) for value in case.expected.values()}


def _self_consistency(spec: SpecUnderTest, suite: TestSuite) -> tuple[float, list[str]]:
    """Share of cases whose own claims match their own inputs."""
    if not suite.cases:
        return 0.0, []
    failures: list[str] = []
    for case in suite.cases:
        reason = _case_defect(spec, case)
        if reason:
            failures.append(f"{case.name}: {reason}")
    return 1.0 - len(failures) / len(suite.cases), failures


def _case_defect(spec: SpecUnderTest, case: TestCase) -> str | None:
    undeclared = [name for name in case.inputs if spec.param(name) is None]
    if undeclared:
        return f"passes parameters the spec does not declare: {sorted(undeclared)}"

    if case.kind == "happy_path":
        missing = [param.name for param in spec.required_params() if param.name not in case.inputs]
        if missing:
            return f"happy path omits required parameters {missing}"
        bad = [name for name, value in case.inputs.items() if not _param_of(spec, name).satisfies(value)]
        if bad:
            return f"happy path passes illegal values for {sorted(bad)}"
        return None

    if case.kind == "boundary":
        param = spec.param(case.targets_param or "")
        if param is None:
            return "boundary case names no declared parameter"
        if case.targets_param not in case.inputs:
            return f"boundary case does not pass {case.targets_param}"
        observed = param.role_of(case.inputs[case.targets_param])
        if observed is None:
            return f"value for {case.targets_param} sits on no boundary"
        if case.boundary_role and case.boundary_role != observed:
            return f"claims role {case.boundary_role} but the value is {observed}"
        return None

    if case.kind == "negative":
        missing_required = any(param.name not in case.inputs for param in spec.required_params())
        illegal = any(not _param_of(spec, name).satisfies(value) for name, value in case.inputs.items())
        if not (missing_required or illegal):
            return "negative case passes an entirely legal input"
        return None

    if case.kind == "error_path":
        if not case.targets_error:
            return "error-path case names no error"
        if spec.error(case.targets_error) is None:
            return f"targets undocumented error {case.targets_error}"
        if not _expects_code(case, case.targets_error):
            return f"targets error {case.targets_error} but does not expect it"
        return None

    if spec.idempotent is None:
        return "spec says nothing about repetition, so this case asserts nothing"
    if case.repeat < 2:
        return "idempotency case does not repeat the call"
    return None


def _param_of(spec: SpecUnderTest, name: str) -> ParamSpec:
    param = spec.param(name)
    if param is None:
        raise KeyError(f"spec {spec.id!r} declares no parameter {name!r}")
    return param


def _weakest_axis(axis_scores: dict[str, float], *, skewed: bool, missing_kinds: list[str]) -> str:
    """The axis most worth working on next, or "none" when nothing is missing.

    `kind_balance` only competes once the suite is past the skew threshold or has
    an applicable kind at zero. Below that, an uneven-but-complete spread is not
    a defect — a spec with three constrained parameters *should* have more
    boundary cases than happy-path ones, and reporting that as the weakest axis
    would send an author to rebalance a suite that has nothing wrong with it.
    """
    candidates = ["boundary_coverage", "error_coverage", "param_coverage"]
    if skewed or missing_kinds:
        candidates.insert(0, "kind_balance")
    weakest = min(candidates, key=lambda axis: (axis_scores[axis], AXES.index(axis)))
    if axis_scores[weakest] >= 1.0 and not skewed and not missing_kinds:
        return "none"
    return weakest


def _recommend(report: CoverageReport) -> str:
    """One plain sentence naming what to add next."""
    axis = report.weakest_axis
    if axis == "none":
        return "四个维度都覆盖到了，没有需要先补的缺口。"

    if axis == "kind_balance":
        share = int(round(report.dominant_share * 100))
        if report.missing_kinds:
            return (
                f"用例类型不均衡：{report.dominant_kind} 占了 {share}%，"
                f"完全没有 {'、'.join(report.missing_kinds)} 这类用例，先各补一条。"
            )
        return f"用例类型不均衡：{report.dominant_kind} 占了 {share}%，把其他类型补到接近的数量。"

    if axis == "boundary_coverage":
        if report.boundary_gaps:
            gap = report.boundary_gaps[0]
            return f"参数 {gap.param} 的边界值还差 {'、'.join(gap.missing)}，补上再看。"
        return "边界值还没覆盖全，先按最小值、最大值、刚好在内、刚好越界各补一条。"

    if axis == "error_coverage":
        if report.uncovered_errors:
            return f"文档写了的错误 {'、'.join(report.uncovered_errors)} 还没有对应用例，各补一条。"
        return "错误路径的用例没有断言到对应的错误码，先把期望值改对。"

    if report.unexercised_params:
        return f"这些参数一次都没传过：{'、'.join(report.unexercised_params)}，先各给一条用例。"
    if report.default_only_params:
        return f"这些参数只传过默认值：{'、'.join(report.default_only_params)}，换个值再测一次。"
    return "参数覆盖不全，检查一下有没有参数从来没被真正取过值。"


def summarise(reports: list[CoverageReport]) -> dict[str, Any]:
    """Aggregate several reports into one row set plus means."""
    if not reports:
        return {"suites": 0}

    def mean(name: str) -> float:
        return round(sum(getattr(item, name) for item in reports) / len(reports), 4)

    return {
        "suites": len(reports),
        "skew": mean("skew"),
        "skewed_suites": [item.spec_id for item in reports if item.skewed],
        "param_coverage": mean("param_coverage"),
        "boundary_coverage": mean("boundary_coverage"),
        "error_coverage": mean("error_coverage"),
        "self_consistency": mean("self_consistency"),
    }
