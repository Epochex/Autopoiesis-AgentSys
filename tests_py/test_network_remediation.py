"""The refusals matter more than the happy paths.

Every test below is a case where acting would be wrong: the target is healthy,
the target is the remote-work path, the unit died on a dependency, or the
restart budget is spent. An implementation that only gets the happy path right
is the one that causes the outage.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from domains.network_rca.remediation import (
    Command,
    UnsafeTarget,
    assert_not_tailscale,
    bounce_interface,
    dependency_failure,
    interface_probe,
    read_interface,
    read_unit,
    restart_unit,
    unit_probe,
)


def _fake(responses: dict[str, str], record: list[list[str]] | None = None) -> Command:
    """A Command that answers by argv prefix and records what it was asked to run."""

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        if record is not None:
            record.append(argv)
        joined = " ".join(argv)
        for prefix, out in responses.items():
            if joined.startswith(prefix):
                return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return Command(run=run)


# ── the protected path ───────────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["tailscale0", "tailscaled", "tailscaled.service", "100.64.1.9"])
def test_tailscale_targets_are_refused(target):
    with pytest.raises(UnsafeTarget):
        assert_not_tailscale(target)


def test_bounce_refuses_the_tailscale_interface_before_running_anything():
    ran: list[list[str]] = []
    command = _fake({}, ran)
    with pytest.raises(UnsafeTarget):
        bounce_interface(command, "tailscale0")
    assert ran == [], "refusal must happen before any command reaches the host"


def test_ordinary_targets_pass_the_check():
    for target in ("eno1", "enp3s0", "autopoiesis-facts-ingest", "192.168.1.23"):
        assert_not_tailscale(target)


# ── interface ────────────────────────────────────────────────────────────────


def test_bounce_refuses_an_interface_that_is_carrying():
    ran: list[list[str]] = []
    command = _fake({"ip -br link show eno1": "eno1  UP  aa:bb:cc:dd:ee:ff"}, ran)
    with pytest.raises(UnsafeTarget, match="not down"):
        bounce_interface(command, "eno1")
    assert not any("set" in " ".join(argv) for argv in ran), "a live NIC must not be touched"


def test_bounce_downs_then_ups_a_dead_interface():
    ran: list[list[str]] = []
    command = _fake({"ip -br link show eno1": "eno1  DOWN  aa:bb:cc:dd:ee:ff"}, ran)
    carrier = bounce_interface(command, "eno1")
    issued = [" ".join(argv) for argv in ran]
    assert "ip link set eno1 down" in issued
    assert "ip link set eno1 up" in issued
    assert issued.index("ip link set eno1 down") < issued.index("ip link set eno1 up")
    assert carrier is False  # the fake still reports DOWN, so we report honestly


def test_missing_interface_is_not_treated_as_down():
    command = _fake({})  # empty output, as for a NIC that does not exist
    reading = read_interface(command, "eno9")
    assert reading["found"] is False
    with pytest.raises(UnsafeTarget):
        bounce_interface(command, "eno9")


def test_interface_probe_reads_live_each_call():
    states = iter(["eno1 UP x", "eno1 DOWN x", "eno1 UP x"])
    command = Command(run=lambda argv: subprocess.CompletedProcess(argv, 0, next(states), ""))
    probe = interface_probe(command, "eno1")
    assert probe.sample()[1] is True
    assert probe.sample()[1] is False
    assert probe.sample()[1] is True


# ── systemd ──────────────────────────────────────────────────────────────────


def test_restart_refuses_a_running_unit():
    ran: list[list[str]] = []
    command = _fake({"systemctl is-active": "active"}, ran)
    with pytest.raises(UnsafeTarget, match="not failed"):
        restart_unit(command, "autopoiesis-facts-ingest")
    assert not any("restart" in " ".join(argv) for argv in ran)


def test_restart_stops_once_the_budget_is_spent():
    command = _fake(
        {
            "systemctl is-active": "failed",
            "systemctl show autopoiesis-facts-ingest -p NRestarts": "2",
        }
    )
    with pytest.raises(UnsafeTarget, match="already been restarted"):
        restart_unit(command, "autopoiesis-facts-ingest", max_restarts=2)


def test_restart_declines_when_a_dependency_is_unreachable():
    """A restart loop against a downed dependency turns degradation into outage."""
    command = _fake(
        {
            "systemctl is-active": "failed",
            "systemctl show": "0",
            "journalctl": "Aug 22 01:00 collector: connection refused to 192.168.1.9:9092",
        }
    )
    with pytest.raises(UnsafeTarget, match="unreachable dependency"):
        restart_unit(command, "autopoiesis-facts-ingest")


def test_restart_proceeds_for_a_plain_failure():
    ran: list[list[str]] = []
    command = _fake(
        {
            "systemctl is-active": "failed",
            "systemctl show": "0",
            "journalctl": "Aug 22 01:00 collector: segfault at 0",
        },
        ran,
    )
    restart_unit(command, "autopoiesis-facts-ingest")
    assert any(" ".join(argv) == "systemctl restart autopoiesis-facts-ingest" for argv in ran)


def test_dependency_signatures_are_matched_case_insensitively():
    command = _fake({"journalctl": "No Route To Host"})
    assert dependency_failure(command, "x") is True


def test_unit_probe_reports_running_state():
    command = _fake({"systemctl is-active": "active", "systemctl show": "1"})
    reading, healthy = unit_probe(command, "autopoiesis-facts-ingest").sample()
    assert healthy is True
    assert reading["restarts"] == 1


def test_read_unit_defaults_restarts_when_unparseable():
    command = _fake({"systemctl is-active": "failed", "systemctl show": ""})
    assert read_unit(command, "x")["restarts"] == 0


# ── end to end through the gateway layer ─────────────────────────────────────


def _host(recovers: bool = True, gateway_fails_after_baseline: bool = False) -> Command:
    """A host whose interface only comes up once the `up` command actually runs."""
    state = {"up": False, "curls": 0}

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        joined = " ".join(argv)
        if joined == "ip link set eth9 up" and recovers:
            state["up"] = True
        if joined.startswith("ip -br link show"):
            status = "UP" if state["up"] else "DOWN"
            return subprocess.CompletedProcess(argv, 0, f"eth9  {status}  aa:bb", "")
        if joined.startswith("curl"):
            state["curls"] += 1
            healthy = not (gateway_fails_after_baseline and state["curls"] > 1)
            return subprocess.CompletedProcess(argv, 0, "200" if healthy else "000", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return Command(run=run)


def _bake():
    from core.remediate import BakeIn

    return BakeIn(window_seconds=30.0, interval_seconds=10.0, grace_seconds=0.0)


def test_end_to_end_recovery_passes_the_window():
    from frontend.gateway.app import remediation as gateway

    result = gateway.execute("bounce_interface", "eth9", command=_host(), bake_in=_bake(), sleep=lambda _s: None)
    assert result["ran"] is True
    assert result["verdict"]["outcome"] == "passed"
    assert result["verdict"]["baseline"] == {"carrier:eth9": False, "gateway": True}
    assert result["needs_human"] is False
    assert result["recovery_run"]["state"] == "passed"
    assert result["recovery_run"]["action_budget"] == 2
    assert result["budget_decision"]["allowed"] is True


def test_end_to_end_collateral_damage_is_caught_even_though_the_target_recovered():
    """The NIC came back, and the gateway went down with it. That is not a pass."""
    from frontend.gateway.app import remediation as gateway

    result = gateway.execute(
        "bounce_interface", "eth9",
        command=_host(gateway_fails_after_baseline=True), bake_in=_bake(), sleep=lambda _s: None,
    )
    assert result["verdict"]["regressed_probes"] == ["gateway"]
    assert result["needs_human"] is True
    assert result["verdict"]["outcome"] != "passed"


def test_commit_recheck_blocks_a_target_that_healed_between_preflight_and_commit():
    """Preflight said eligible; by commit time the NIC is carrying again."""
    from frontend.gateway.app import remediation as gateway

    readings = iter(["eth9  DOWN  aa:bb"] + ["eth9  UP  aa:bb"] * 12)

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        if " ".join(argv).startswith("ip -br link show"):
            return subprocess.CompletedProcess(argv, 0, next(readings), "")
        return subprocess.CompletedProcess(argv, 0, "200", "")

    result = gateway.execute("bounce_interface", "eth9", command=Command(run=run), bake_in=_bake(), sleep=lambda _s: None)
    assert result["ran"] is False
    assert result["refused"] is True
    assert "not down" in result["reason"]


def test_unknown_action_is_refused():
    from domains.network_rca.remediation import UnsafeTarget
    from frontend.gateway.app import remediation as gateway

    with pytest.raises(UnsafeTarget, match="closed list"):
        gateway.preflight("rm_rf", "/")


def test_durable_emergency_stop_blocks_before_any_host_command():
    from frontend.gateway.app import remediation as gateway

    ran: list[list[str]] = []
    control = gateway.emergency_stop()
    control.pause("management plane maintenance", "operator-test")
    try:
        result = gateway.execute(
            "bounce_interface",
            "eth9",
            command=_fake({}, ran),
            bake_in=_bake(),
            sleep=lambda _s: None,
        )
    finally:
        control.resume("operator-test", "test cleanup")
    assert result["ran"] is False
    assert result["refused"] is True
    assert "global remediation pause" in result["reason"]
    assert ran == []


def test_duplicate_execution_id_is_refused_without_a_second_write():
    from frontend.gateway.app import remediation as gateway

    first = gateway.execute(
        "bounce_interface",
        "eth9",
        command=_host(),
        bake_in=_bake(),
        sleep=lambda _s: None,
        incident_id="incident-idempotency",
        failure_domain="redundancy-pair-a",
        idempotency_key="request-1",
    )
    second = gateway.execute(
        "bounce_interface",
        "eth9",
        command=_host(),
        bake_in=_bake(),
        sleep=lambda _s: None,
        incident_id="incident-idempotency",
        failure_domain="redundancy-pair-a",
        idempotency_key="request-1",
    )
    assert first["ran"] is True
    assert second["ran"] is False
    assert second["budget_decision"]["idempotent"] is True
    assert second["reason"] == "duplicate_execution"


def test_startup_reconciles_an_interrupted_budget_reservation(tmp_path, monkeypatch):
    from core.remediate.safety import RemediationBudget
    from frontend.gateway.app import remediation as gateway

    prior = RemediationBudget(cooldown_seconds=0, backoff_base_seconds=0, backoff_max_seconds=0)
    acquired = prior.acquire("incident-crash", "asset-a", "domain-a", "restart_unit")
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(prior.to_dict()), encoding="utf-8")
    monkeypatch.setenv("AUTOPOIESIS_REMEDIATION_BUDGET", str(path))
    monkeypatch.setattr(gateway, "_BUDGET", None)
    monkeypatch.setattr(gateway, "_BUDGET_LOAD_ERROR", None)

    loaded = gateway._load_budget()

    assert loaded.in_flight_execution_ids() == ()
    row = next(
        row for row in loaded.to_dict()["records"]
        if row["execution_id"] == acquired.execution_id
    )
    assert row["success"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["completed_at"] is not None
