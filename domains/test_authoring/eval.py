"""Measuring the test author without paying to measure it.

What is under test here is not a model's prose. It is whether a suite covers
the thing it claims to cover, and every part of that is a property of data: the
kinds it spreads across, the parameters it actually varies, the boundary
positions its values occupy, the documented failures it provokes. All of it can
be scored against recorded suites, so the default run costs nothing and can sit
in CI. A live run exists for the one question fixtures cannot answer — whether
a real model, handed this spec, writes cases a schema could not have implied —
and it is opt-in and capped.

The fixture set is deliberately uneven, because an eval whose fixtures all
score 1.0 cannot tell you the scorer works:

``gateway_rca_cost`` (recorded) stops at min and max, and reaches for 1000 as
its out-of-range value — far enough past the bound that it exercises the same
branch a type error does. Its boundary axis has to come back at 0.5.

``gateway_remediation_preflight`` (recorded) contains a case that calls itself
an out-of-range test while passing a perfectly legal action, and one that
asserts a 500 the endpoint never documents. Both must be named, not silently
absorbed.

``gateway_investigate_run_step`` (recorded) is the balanced control.

``gateway_remediation_execute`` (recorded) covers every boundary and every
error code and never repeats a call — on the one endpoint in the set that is
explicitly not safe to repeat.

And the designed negative: ten cases against the cost endpoint, every one of
them internally valid, nine of them the same happy path. It would pass a test
run end to end. If it ever scores well here, this file has stopped working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from core.orchestrator.planner import execute_chain
from domains.test_authoring.adapters.spec_library import SpecLibrary
from domains.test_authoring.coverage import CoverageReport, score_suite, summarise
from domains.test_authoring.factory import (
    build_spec_library,
    build_test_authoring_intent_router,
    build_test_authoring_orchestrator,
    load_test_authoring_seed_cases,
)
from domains.test_authoring.generator import generate_suite
from domains.test_authoring.schema import SpecUnderTest, TestAuthoringCase, TestSuite


ROOT = Path(__file__).resolve().parent
FIXTURE_PATH = ROOT / "fixtures" / "recorded_suites.json"

# A live run is opt-in and capped. The cap is enforced here rather than trusted
# to the caller, because "just one more spec" is how an eval becomes a bill.
LIVE_CALL_CEILING = int(os.getenv("AUTOPOIESIS_EVAL_MAX_CALLS", "6"))

# Below this on any single axis, a suite is reported as a failure even if its
# cases all pass. Set where a genuinely incomplete axis lands, not where a
# perfect one does.
PASS_FLOOR = 0.6


def load_recorded_suites(path: Path | None = None) -> list[tuple[TestSuite, str]]:
    """Recorded model suites, each with the one-line note on what is wrong with it."""
    payload = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    return [(TestSuite.model_validate(item), str(item.get("flaw", ""))) for item in payload["suites"]]


def load_designed_negative(path: Path | None = None) -> tuple[TestSuite, str]:
    """The suite that passes every test it contains and is still badly skewed."""
    payload = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    item = payload["designed_negative"]
    return TestSuite.model_validate(item), str(item.get("why", ""))


def run_case(spec: SpecUnderTest, suite: TestSuite) -> CoverageReport:
    """Score one suite against one spec."""
    return score_suite(spec, suite)


def run(
    *,
    live: bool = False,
    client_factory: Callable[[], Any] | None = None,
    library: SpecLibrary | None = None,
) -> dict[str, Any]:
    """Score the deterministic suites, the recorded suites and the designed negative.

    ``live=True`` spends money: one call per spec, capped at LIVE_CALL_CEILING.
    Everything else runs against the fixtures and costs nothing.
    """
    library = library or build_spec_library()
    specs = library.specs()

    client = None
    if live:
        if len(specs) > LIVE_CALL_CEILING:
            raise ValueError(
                f"{len(specs)} specs exceeds the live ceiling of {LIVE_CALL_CEILING}; "
                "raise AUTOPOIESIS_EVAL_MAX_CALLS deliberately or run offline"
            )
        factory = client_factory or _default_client
        client = factory()
        if client is None:
            raise RuntimeError("live run requested but no model is configured")

    generated = [run_case(spec, generate_suite(spec, model=client)) for spec in specs]
    recorded = [run_case(library.spec(suite.spec_id), suite) for suite, _ in load_recorded_suites()]

    negative_suite, negative_why = load_designed_negative()
    negative = run_case(library.spec(negative_suite.spec_id), negative_suite)

    scored = generated + recorded
    return {
        "cases": len(scored),
        "mode": "live" if live else "offline",
        "generated": summarise(generated),
        "recorded": summarise(recorded),
        "designed_negative": {
            **_row(negative),
            "why": negative_why,
            # The whole point of the control: everything it contains is valid.
            "self_consistency": negative.self_consistency,
        },
        "designed_negative_caught": not negative.is_acceptable(floor=PASS_FLOOR),
        "failures": [_row(report) for report in scored if not report.is_acceptable(floor=PASS_FLOOR)],
        "rows": [_row(report) for report in scored],
    }


def run_routed_eval(induction_store: str | Path | None = None) -> list[dict[str, Any]]:
    """Route every seed case through the kernel, then author from what it collected.

    This is the portability check rather than a quality one: the same
    CascadingIntentRouter, the same chain executor and the same contract
    verifier that run enterprise ops and network RCA also run this domain, and
    the evidence the chain produces is what the generator consumes.
    `unmatched_authoring_case` matches nothing and exercises the
    miss -> capture -> induce -> promote tier.
    """
    rows: list[dict[str, Any]] = []
    with TemporaryDirectory() as tmp_dir:
        orchestrator = build_test_authoring_orchestrator(Path(tmp_dir) / "routed_trace.jsonl")
        router = build_test_authoring_intent_router(
            orchestrator,
            induction_store=Path(induction_store)
            if induction_store is not None
            else Path(tmp_dir) / "induction_captures.jsonl",
        )
        library = orchestrator.system_adapter
        for case in [*load_test_authoring_seed_cases(), unmatched_authoring_case()]:
            outcome = router.route(case)
            row: dict[str, Any] = {
                "case_id": case.id,
                "tier": outcome.tier,
                "resolved": outcome.resolved,
                "induced": outcome.induced,
                "chain": list(outcome.chain),
                "executed": False,
                "violations": [],
            }
            if outcome.resolved and outcome.chain and library.has_case(case.id):
                result = execute_chain(outcome.chain, case, orchestrator)
                evidence = [
                    item
                    for step_result, verdict in zip(result["results"], result["verdicts"])
                    if verdict.passed
                    for item in step_result.evidence
                ]
                spec = library.spec_for(case.id)
                report = run_case(spec, generate_suite(spec, evidence=evidence))
                row["executed"] = True
                row["evidence_kinds"] = [item.get("kind") for item in evidence]
                row["violations"] = [
                    violation for verdict in result["verdicts"] for violation in verdict.violations
                ]
                row["coverage"] = _row(report)
            rows.append(row)
    return rows


def unmatched_authoring_case() -> TestAuthoringCase:
    """A request no analysis skill covers — drives the miss->induction tier."""
    return TestAuthoringCase(
        id="authoring_unmatched_load_profile",
        query="warehouse restock inventory plan",
        query_terms=["warehouse", "restock", "inventory"],
        assets=["warehouse-88"],
        relevant_skills=[],
    )


def _row(report: CoverageReport) -> dict[str, Any]:
    return {
        "spec_id": report.spec_id,
        "source": report.source,
        "cases": report.total_cases,
        "skew": report.skew,
        "skewed": report.skewed,
        "dominant": f"{report.dominant_kind} {int(round(report.dominant_share * 100))}%",
        "missing_kinds": report.missing_kinds,
        "param_coverage": report.param_coverage,
        "boundary_coverage": report.boundary_coverage,
        "error_coverage": report.error_coverage,
        "self_consistency": report.self_consistency,
        "weakest_axis": report.weakest_axis,
        "recommendation": report.recommendation,
    }


def _default_client():
    from frontend.gateway.app.model_access import client_for

    return client_for("eval", timeout_sec=120)


def _print_report(report: dict[str, Any]) -> None:
    print(f"mode={report['mode']} suites={report['cases']}")
    print(f"deterministic: {report['generated']}")
    print(f"recorded:      {report['recorded']}")
    print()
    for row in report["rows"]:
        flag = "SKEWED" if row["skewed"] else "ok"
        print(
            f"{row['spec_id']:<32} {row['source']:<26} cases={row['cases']:>3} "
            f"skew={row['skew']:.3f} [{flag}] weakest={row['weakest_axis']}"
        )
        print(f"    {row['recommendation']}")
    print()
    negative = report["designed_negative"]
    print("designed negative (should never look good):")
    print(
        f"  {negative['spec_id']} cases={negative['cases']} "
        f"self_consistency={negative['self_consistency']} skew={negative['skew']} "
        f"skewed={negative['skewed']} caught={report['designed_negative_caught']}"
    )
    print(f"  {negative['recommendation']}")
    print()


def _print_routed_rows(rows: list[dict[str, Any]]) -> None:
    print("cascading intent routing:")
    for row in rows:
        chain = " -> ".join(row["chain"]) if row["chain"] else "-"
        print(
            f"case={row['case_id']} tier={row['tier']} resolved={row['resolved']} "
            f"induced={row['induced']} chain=[{chain}] executed={row['executed']}"
        )
        if row["violations"]:
            print(f"  caught violations: {row['violations']}")
        if row.get("coverage"):
            coverage = row["coverage"]
            print(
                f"  authored {coverage['cases']} cases, skew={coverage['skew']}, "
                f"weakest={coverage['weakest_axis']}"
            )
    print()


if __name__ == "__main__":
    _print_report(run())
    _print_routed_rows(run_routed_eval())
