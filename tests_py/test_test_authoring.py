"""The test-authoring domain, and the scorer that decides whether a suite is any good.

Two things are being guarded here. The first is that the kernel is portable:
the same router, chain executor and contract verifier that run enterprise ops
and network RCA also run a domain built out of nothing but a skill registry, a
retrieval pass and contracts — no weights were touched to make it work.

The second is that the scorer measures skew rather than pass rate. Most of
these cases hand it a suite that would go green end to end and check that it
still comes back with the gap named. A scorer that reports 1.0 on those is not
strict, it is broken.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.orchestrator.planner import execute_chain, plan_skill_chain
from domains.test_authoring.adapters.spec_library import SpecLibrary
from domains.test_authoring.coverage import (
    SKEW_THRESHOLD,
    applicable_kinds,
    score_suite,
)
from domains.test_authoring.eval import (
    load_designed_negative,
    load_recorded_suites,
    run,
    run_routed_eval,
)
from domains.test_authoring.factory import (
    SPECS_PATH,
    build_spec_library,
    build_test_authoring_intent_router,
    build_test_authoring_orchestrator,
    load_test_authoring_seed_cases,
)
from domains.test_authoring.generator import generate_suite
from domains.test_authoring.schema import TestCase, TestSuite
from domains.test_authoring.skills.authoring_skills import (
    EVIDENCE_SIMILAR,
    recall_similar_specs,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _library():
    return build_spec_library()


def _recorded(spec_id: str) -> TestSuite:
    for suite, _ in load_recorded_suites():
        if suite.spec_id == spec_id:
            return suite
    raise AssertionError(f"no recorded suite for {spec_id}")


# --- the kernel runs this domain unchanged ------------------------------------


def test_an_authoring_request_routes_and_executes_on_the_unmodified_kernel(tmp_path):
    """The portability claim, checked rather than asserted in prose.

    If this domain needed a bespoke planner or its own execution loop, the claim
    that verticality comes from skills and contracts instead of a fine-tune
    would be empty.
    """
    orchestrator = build_test_authoring_orchestrator(tmp_path / "authoring_trace.jsonl")
    case = load_test_authoring_seed_cases()[0]

    result = execute_chain(plan_skill_chain(case.query, orchestrator.skills), case, orchestrator)

    assert result["chain"] == ["analyze_signature", "find_boundaries"]
    assert [verdict.passed for verdict in result["verdicts"]] == [True, True]
    assert result["completed"] is True


def test_every_seed_request_resolves_and_the_unmatched_one_induces():
    """Five requests, five specs, all five skills — plus one nothing covers.

    The miss tier is what stops the domain quietly answering an off-topic
    request with whichever skill happened to share a word with it.
    """
    rows = run_routed_eval()

    resolved = [row for row in rows if row["case_id"] != "authoring_unmatched_load_profile"]
    assert all(row["tier"] == "rule_fast_path" for row in resolved)
    assert all(row["executed"] for row in resolved)
    assert not any(row["violations"] for row in resolved)

    miss = next(row for row in rows if row["case_id"] == "authoring_unmatched_load_profile")
    assert miss["tier"] == "skill_induction"
    assert miss["executed"] is False


def test_every_authoring_skill_is_read_only(tmp_path):
    """This domain describes systems; it must never be able to touch one.

    A write-risk skill here would put the kernel's approval gate between a test
    author and production, which is not a place that gate should ever be load
    bearing.
    """
    orchestrator = build_test_authoring_orchestrator(tmp_path / "risk_trace.jsonl")

    risks = {skill.spec.name: skill.spec.risk for skill in orchestrator.skills.all()}

    assert set(risks) == {
        "analyze_signature",
        "find_boundaries",
        "find_error_paths",
        "check_idempotency",
        "recall_similar_specs",
    }
    assert set(risks.values()) == {"read_only"}


def test_the_intent_router_keeps_every_seed_route_as_a_golden_case(tmp_path):
    """Skill promotion must not be allowed to steal an existing route."""
    orchestrator = build_test_authoring_orchestrator(tmp_path / "router_trace.jsonl")
    router = build_test_authoring_intent_router(orchestrator)

    expected = {case.relevant_skills[0] for case in load_test_authoring_seed_cases()}

    assert {item["expected_skill"] for item in router.golden_cases} == expected


# --- skew is the thing being measured -----------------------------------------


def test_a_lopsided_suite_is_caught_even_though_every_case_is_valid():
    """Twelve happy-path calls would go green twelve times and prove nothing.

    This is the failure the domain exists to catch: a pass rate of 1.0 on a
    suite that never once crosses a boundary, omits a parameter, or provokes a
    documented failure.
    """
    spec = _library().spec("gateway_investigate_run_step")
    suite = TestSuite(
        spec_id=spec.id,
        source="lopsided",
        cases=[
            TestCase(
                name=f"only_happy_{step}",
                kind="happy_path",
                inputs={"session_id": "sess-7f3a91", "step": step},
                expected={"status": 200},
                rationale="又一条正常路径。",
            )
            for step in range(3, 15)
        ],
    )

    report = score_suite(spec, suite)

    assert report.self_consistency == 1.0
    assert report.skew == 1.0
    assert report.skewed is True
    assert report.is_acceptable() is False
    assert report.missing_kinds == ["boundary", "negative", "error_path"]


def test_a_balanced_suite_is_not_flagged():
    """The scorer has to be able to say yes, or the threshold means nothing."""
    spec = _library().spec("gateway_investigate_run_step")

    report = score_suite(spec, _recorded(spec.id))

    assert report.skew < SKEW_THRESHOLD
    assert report.skewed is False
    assert report.error_coverage == 1.0
    assert report.is_acceptable() is True


def test_skew_is_not_a_demand_for_an_even_split():
    """A spec with several constrained parameters needs more boundary cases.

    Marking that down would push an author to delete boundary coverage in order
    to look balanced, which is the opposite of the point.
    """
    spec = _library().spec("gateway_investigate_start")

    report = score_suite(spec, generate_suite(spec))

    assert report.dominant_kind == "boundary"
    assert report.dominant_share > 0.5
    assert report.skewed is False
    assert report.weakest_axis == "none"


# --- boundary coverage is read off the values, not the labels -----------------


def test_boundary_coverage_notices_a_missing_just_outside_case():
    """min and max alone are the boundary test everyone writes and it is half of one.

    The off-by-one lives between just-inside and just-outside; a suite that
    stops at the two named bounds never crosses it.
    """
    spec = _library().spec("gateway_rca_cost")

    report = score_suite(spec, _recorded(spec.id))

    gap = next(item for item in report.boundary_gaps if item.param == "hours")
    assert gap.present == ["min", "max"]
    assert gap.missing == ["just_inside", "just_outside"]
    assert report.boundary_coverage == 0.5
    assert report.weakest_axis == "boundary_coverage"


def test_a_value_far_past_the_bound_is_not_a_boundary_test():
    """hours=1000 against a ceiling of 720 exercises the same branch a type error does.

    Crediting it as an out-of-range boundary case would let a suite claim
    coverage of an off-by-one it never went near.
    """
    spec = _library().spec("gateway_rca_cost")
    hours = spec.param("hours")

    assert hours.role_of(1000) is None
    assert hours.role_of(721) == "just_outside"
    assert hours.role_of(0) == "just_outside"


def test_a_case_that_mislabels_its_own_boundary_earns_nothing():
    """A case is not a boundary test because it says it is.

    The recorded preflight suite contains one that calls itself an out-of-range
    check while passing a perfectly ordinary action name. Trusting the label
    would hand it credit for the case it failed to write.
    """
    spec = _library().spec("gateway_remediation_preflight")

    report = score_suite(spec, _recorded(spec.id))

    assert report.mislabelled_boundaries == ["preflight_boundary_action_outside"]
    gap = next(item for item in report.boundary_gaps if item.param == "action")
    assert "just_outside" in gap.missing
    assert any("sits on no boundary" in item for item in report.inconsistent_cases)


def test_an_unreachable_boundary_role_is_never_reported_as_a_gap():
    """`lang` has two members, so it has no interior value anyone could pass.

    Demanding a just-inside case for it would be a gap no author can ever close,
    and a permanently unclosable gap trains people to ignore the report.
    """
    lang = _library().spec("gateway_investigate_start").param("lang")

    assert lang.required_boundary_roles() == ("min", "max", "just_outside")
    assert lang.role_of("zh") == "min"
    assert lang.role_of("fr") == "just_outside"


# --- error paths, parameters, and axes that do not apply ----------------------


def test_an_error_the_spec_never_documents_is_not_coverage():
    """Asserting a 500 nobody promised is a case that can only ever fail."""
    spec = _library().spec("gateway_remediation_preflight")

    report = score_suite(spec, _recorded(spec.id))

    assert report.phantom_errors == ["preflight_error_phantom"]
    assert report.uncovered_errors == ["422"]
    assert report.error_coverage == 0.5


def test_targeting_an_error_without_expecting_it_earns_nothing():
    """A case that provokes a 422 and asserts a 200 proves the opposite of its label."""
    spec = _library().spec("gateway_rca_cost")
    suite = TestSuite(
        spec_id=spec.id,
        source="wrong_expectation",
        cases=[
            TestCase(
                name="cost_error_mislabelled",
                kind="error_path",
                inputs={"hours": 1000},
                expected={"status": 200},
                rationale="想测越界，断言却写成了成功。",
                targets_error="422",
            )
        ],
    )

    report = score_suite(spec, suite)

    assert report.error_coverage == 0.0
    assert report.uncovered_errors == ["422"]
    assert report.inconsistent_cases == [
        "cost_error_mislabelled: targets error 422 but does not expect it"
    ]


def test_a_parameter_only_ever_passed_its_default_is_not_under_test():
    """If every case passes hours=24, the suite would score the same with no hours at all."""
    spec = _library().spec("gateway_rca_cost")
    suite = TestSuite(
        spec_id=spec.id,
        source="default_only",
        cases=[
            TestCase(
                name=f"cost_default_{index}",
                kind="happy_path",
                inputs={"hours": 24},
                expected={"status": 200},
                rationale="又一次默认窗口。",
            )
            for index in range(3)
        ],
    )

    report = score_suite(spec, suite)

    assert report.default_only_params == ["hours"]
    assert report.param_coverage == 0.0


def test_an_axis_the_spec_says_nothing_about_is_not_scored_as_a_gap():
    """run-step is silent on repetition; execute explicitly is not.

    Treating silence as a failure would make the report unfixable for one
    endpoint and would hide the real omission on the other.
    """
    library = _library()

    silent = applicable_kinds(library.spec("gateway_investigate_run_step"))
    declared = applicable_kinds(library.spec("gateway_remediation_execute"))

    assert "idempotency" not in silent
    assert "idempotency" in declared

    report = score_suite(library.spec("gateway_remediation_execute"), _recorded("gateway_remediation_execute"))
    assert report.missing_kinds == ["idempotency"]
    assert "idempotency" in report.recommendation


def test_scoring_a_suite_against_the_wrong_spec_is_refused():
    """Pairing them silently produces a confident, meaningless scorecard."""
    library = _library()
    suite = _recorded("gateway_rca_cost")

    with pytest.raises(ValueError, match="was scored against"):
        score_suite(library.spec("gateway_investigate_start"), suite)


# --- generation without a model, and retrieval that stays separable -----------


def test_the_deterministic_generator_covers_every_applicable_kind():
    """No model, no fine-tune: the boundaries and failures are already in the spec.

    This is what makes the eval free to run, and it is the honest answer to
    whether a vertical domain needs its own weights.
    """
    for spec in _library().specs():
        suite = generate_suite(spec)
        present = {case.kind for case in suite.cases}
        assert set(applicable_kinds(spec)) <= present, spec.id

        report = score_suite(spec, suite)
        assert report.self_consistency == 1.0, (spec.id, report.inconsistent_cases)
        assert report.boundary_coverage == 1.0, (spec.id, report.boundary_gaps)
        assert report.error_coverage == 1.0, (spec.id, report.uncovered_errors)
        assert report.skewed is False, (spec.id, report.skew)


def test_recalled_knowledge_is_attributed_and_never_rewrites_an_assertion():
    """A lesson from a neighbouring endpoint is a hint, not an authority.

    Letting retrieval change what a case expects is how a stale note becomes a
    wrong test that nobody can trace back to its source.
    """
    library = _library()
    spec = library.spec("gateway_remediation_preflight")
    evidence = [
        {
            "evidence_id": "ev-recall",
            "kind": EVIDENCE_SIMILAR,
            "matches": recall_similar_specs(spec, library.corpus_for(spec.id)),
        }
    ]

    plain = generate_suite(spec)
    recalled = generate_suite(spec, evidence=evidence)

    def assertions(suite):
        return [(case.name, case.kind, case.inputs, case.expected) for case in suite.cases]

    assert assertions(plain) == assertions(recalled)
    assert plain.notes == []
    assert any("gateway_remediation_execute" in note for note in recalled.notes)


def test_the_specs_are_read_from_the_real_gateway_rather_than_invented():
    """A fixture that drifts from the endpoint it describes scores the wrong thing.

    These constraints are copied from live Pydantic models. When the gateway
    changes one, this fails — which is the only way anyone finds out the spec
    library went stale.
    """
    library = _library()
    for spec in library.specs():
        assert spec.source is not None, spec.id
        source = REPO_ROOT / spec.source.file
        assert source.exists(), spec.source.file
        assert spec.source.symbol in source.read_text(encoding="utf-8"), spec.id

    gateway = (REPO_ROOT / "frontend" / "gateway" / "app" / "main.py").read_text(encoding="utf-8")
    assert re.search(r"hours: int = Query\(default=24, ge=1, le=720\)", gateway)
    assert re.search(r"step: int = Field\(ge=1, le=99\)", gateway)

    hours = library.spec("gateway_rca_cost").param("hours")
    assert (hours.default, hours.minimum, hours.maximum) == (24, 1, 720)
    step = library.spec("gateway_investigate_run_step").param("step")
    assert (step.minimum, step.maximum) == (1, 99)


# --- the eval itself -----------------------------------------------------------


def test_the_designed_negative_fails_as_intended():
    """Ten valid cases, nine of them the same happy path.

    It would pass a real test run end to end. If this ever comes back
    acceptable, the scorer has stopped measuring anything the pass rate does not
    already tell you.
    """
    library = _library()
    suite, why = load_designed_negative()
    report = score_suite(library.spec(suite.spec_id), suite)

    assert why
    assert report.self_consistency == 1.0
    assert report.skew > 0.75
    assert report.skewed is True
    assert report.error_coverage == 0.0
    assert report.is_acceptable() is False

    assert run()["designed_negative_caught"] is True


def test_the_deterministic_suites_clear_every_axis_in_the_eval():
    """The floor the recorded suites are measured against has to be reachable."""
    report = run()

    generated = report["generated"]
    assert generated["suites"] == 5
    assert generated["skewed_suites"] == []
    assert generated["param_coverage"] == 1.0
    assert generated["boundary_coverage"] == 1.0
    assert generated["error_coverage"] == 1.0
    assert generated["self_consistency"] == 1.0


def test_the_recorded_suites_are_not_all_perfect():
    """An eval whose fixtures all score 1.0 cannot tell you the scorer works."""
    report = run()

    recorded = report["recorded"]
    assert recorded["boundary_coverage"] < 1.0
    assert recorded["self_consistency"] < 1.0
    assert report["failures"], "every recorded flaw was absorbed silently"


def test_a_live_run_is_capped_rather_than_trusted_to_the_caller(tmp_path):
    """'Just one more spec' is how an eval becomes a bill."""
    payload = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
    payload["specs"] += [
        {**payload["specs"][0], "id": f"filler-{index}"} for index in range(4)
    ]
    oversized = tmp_path / "specs.json"
    oversized.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds the live ceiling"):
        run(live=True, library=SpecLibrary.from_path(oversized))


def test_offline_mode_never_builds_a_client(monkeypatch):
    import domains.test_authoring.eval as eval_module

    def explode():
        raise AssertionError("offline run must not construct a model client")

    monkeypatch.setattr(eval_module, "_default_client", explode)
    assert run()["mode"] == "offline"
