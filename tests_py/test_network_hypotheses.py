from datetime import datetime, timezone

from core.investigate.network_hypotheses import create_network_hypothesis_loop


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
