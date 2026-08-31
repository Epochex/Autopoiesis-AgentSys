from datetime import datetime, timezone

from core.investigate.network_hypotheses import create_network_hypothesis_loop, probe_observation


def test_broad_error_streams_are_evidence_sources_not_root_candidates() -> None:
    loop = create_network_hypothesis_loop(
        case_id="case-host",
        family=None,
        subject="managed-host-a",
        opened_at=datetime.now(timezone.utc),
        question_terms=[],
        ordered_commands=[
            "ip -br link show",
            "ip route show",
            "systemctl --failed --no-legend",
            "journalctl -p err -n 40 --no-pager --since -24h",
            "dmesg -T --level err,crit,alert -x",
        ],
    )

    root_ids = {item.hypothesis_id for item in loop.state.hypotheses}
    assert "service_failed" in root_ids
    assert "system_errors" not in root_ids
    assert "kernel_errors" not in root_ids
    assert "admin_bruteforce_lockout" not in root_ids
    assert "duplicate_ip_static" not in root_ids


def test_named_unit_uses_only_unit_scoped_catalogue_root() -> None:
    loop = create_network_hypothesis_loop(
        case_id="case-unit",
        family=None,
        subject="collector.service",
        opened_at=datetime.now(timezone.utc),
        question_terms=[],
        ordered_commands=["systemctl --failed --no-legend"],
    )

    assert [item.hypothesis_id for item in loop.state.hypotheses] == ["service_failed"]


def test_confirmed_environment_recheck_supports_duplicate_address_root() -> None:
    polarity, decisive, state = probe_observation(
        "duplicate_ip_static",
        {
            "ok": True,
            "output": (
                '{"available":true,"subject":"192.168.1.11",'
                '"fault_class":"duplicate_ip_static",'
                '"verification":{"state":"confirmed"}}'
            ),
        },
        "192.168.1.11",
    )

    assert (polarity, decisive, state) == ("supports", True, "observed")


def test_current_auth_window_supports_distributed_login_attack_root() -> None:
    polarity, decisive, state = probe_observation(
        "admin_bruteforce_lockout",
        {
            "ok": True,
            "output": (
                '{"available":true,"failed_logins":44,'
                '"distinct_sources":12,"lockouts":1}'
            ),
        },
        "192.168.1.1",
    )

    assert (polarity, decisive, state) == ("supports", True, "observed")


def test_neighbor_probe_compares_the_exact_ip_field() -> None:
    polarity, decisive, state = probe_observation(
        "neighbor_unreachable",
        {"ok": True, "output": "192.168.1.110 dev eth2 FAILED\n"},
        "192.168.1.11",
    )

    assert (polarity, decisive, state) == ("opposes", True, "observed")
