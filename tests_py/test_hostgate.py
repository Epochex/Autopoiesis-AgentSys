"""The gate itself, because a gate that fails open is worse than no gate.

Three host-grounded tests were made skippable so CI could be green. That is only
safe if the skip cannot happen on the host those tests exist for — otherwise
unplugging a cable turns a real regression into a silent pass.
"""
from __future__ import annotations

import pytest

import hostgate


def _dataset(monkeypatch, present: bool) -> None:
    monkeypatch.delenv("AUTOPOIESIS_HOST_TESTS", raising=False)
    monkeypatch.setattr(hostgate, "REAL_DATASET", type("P", (), {"exists": lambda _s: present})())


def test_a_met_requirement_leaves_the_test_untouched(monkeypatch):
    _dataset(monkeypatch, True)
    def sample() -> str:
        return "ran"
    assert hostgate._gate(True, "anything")(sample)() == "ran"


def test_off_the_grounded_host_an_unmet_requirement_skips(monkeypatch):
    _dataset(monkeypatch, False)
    mark = hostgate._gate(False, "eth2")
    assert isinstance(mark, pytest.MarkDecorator)
    assert mark.args == (True,), "the skip must be unconditional once the gate is closed"
    assert "eth2" in mark.kwargs["reason"]


def test_on_the_grounded_host_an_unmet_requirement_fails(monkeypatch):
    """The whole point: on R450 a closed gate is host drift, not an environment quirk."""
    _dataset(monkeypatch, True)
    def sample() -> None:
        raise AssertionError("the body must never run")
    with pytest.raises(BaseException, match="host drift"):
        hostgate._gate(False, "eth2 to exist")(sample)()


@pytest.mark.parametrize(
    ("override", "dataset", "strict"),
    [("strict", False, True), ("skip", True, False), ("", True, True), ("", False, False)],
)
def test_the_override_beats_dataset_detection(monkeypatch, override, dataset, strict):
    monkeypatch.setattr(hostgate, "REAL_DATASET", type("P", (), {"exists": lambda _s: dataset})())
    monkeypatch.setenv("AUTOPOIESIS_HOST_TESTS", override)
    assert hostgate._strict() is strict


def test_a_missing_interface_is_decided_by_output_not_exit_code(monkeypatch):
    """`ip -br link show <absent>` exits 0 with empty stdout."""
    class Done:
        stdout = ""
        returncode = 0
    monkeypatch.setattr(hostgate, "_run", lambda _argv: Done())
    assert hostgate._interface_exists("eth99") is False
    assert hostgate._interface_carrier("eth99") is None


def test_absent_tooling_never_masquerades_as_a_met_requirement(monkeypatch):
    monkeypatch.setattr(hostgate, "_run", lambda _argv: None)
    assert hostgate._interface_exists("eth0") is False
    assert hostgate._unit_is_active("anything") is False
    assert hostgate._interface_carrier("eth0") is None


def test_carrier_state_is_read_from_the_link_line(monkeypatch):
    for line, expected in (("eth0 UP aa:bb:cc", True), ("eth0 DOWN aa:bb:cc", False)):
        monkeypatch.setattr(hostgate, "_run", lambda _a, _l=line: type("D", (), {"stdout": _l})())
        assert hostgate._interface_carrier("eth0") is expected
