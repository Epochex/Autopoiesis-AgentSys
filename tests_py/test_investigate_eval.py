"""The harness eval, run as a regression gate.

Offline by default and therefore free, so it can sit in CI. What it guards is
not the model's prose but the properties of the code around it: that the
opening probes surface the fact each question turns on, that a mislabelled
writing command never comes back runnable, and that an invented citation is
always caught.
"""

from __future__ import annotations

import pytest

from core.investigate.eval import load_cases, run, run_case

from hostgate import requires_host_interfaces


def test_the_fixture_set_loads():
    cases = load_cases()
    assert len(cases) >= 5
    assert all(case.question for case in cases)


@requires_host_interfaces("eth1", "eth2")
def test_opening_probes_surface_what_each_question_turns_on():
    """A question whose answer is not in the evidence cannot be answered right."""
    report = run()
    assert report["evidence_sufficiency"] == 1.0, (
        f"missing readings: {[f['missing_evidence'] for f in report['failures'] if f['missing_evidence']]}"
    )


def test_no_writing_command_ever_comes_back_runnable():
    """Four cases hand the harness a mislabelled write. None may earn a button."""
    report = run()
    assert report["grading_integrity"] == 1.0, (
        f"mislabelled as runnable: {[f['mislabelled'] for f in report['failures'] if f['mislabelled']]}"
    )


def test_the_invented_citation_case_is_caught_not_passed():
    """A negative case that stops failing means the check stopped working."""
    case = next(c for c in load_cases() if c.case_id == "hallucinated-citation")
    result = run_case(case)
    assert "ev-099" in result.invented_citations
    assert result.citation_validity < 1.0


def test_context_stays_well_under_sending_everything():
    report = run()
    assert report["context_ratio"] < 0.85, "the budget is not buying anything"


def test_a_live_run_is_capped_rather_than_trusted_to_the_caller():
    """'Just one more variant' is how an eval becomes a bill."""
    cases = load_cases() * 3
    with pytest.raises(ValueError, match="exceeds the live ceiling"):
        run(cases, live=True)


def test_offline_mode_never_builds_a_client(monkeypatch):
    import core.investigate.eval as eval_module

    def explode():
        raise AssertionError("offline run must not construct a model client")

    monkeypatch.setattr(eval_module, "_default_client", explode)
    assert run()["mode"] == "offline"
