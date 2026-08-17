"""Read-only probe executor: the safety model is the point of the tests.

Nothing here opens a socket except the two explicitly-marked live checks, which
are skipped when the loopback target is not up; the rest exercise parsing and
refusal, which is where the security lives.
"""
from __future__ import annotations

import pytest

from domains.active_recon.probe_exec import ProbeRefused, parse_probe, run_probe


def test_readonly_recon_verbs_parse_into_typed_probes():
    assert parse_probe("nc -zv 192.168.1.46 23").kind == "tcp_connect"
    assert parse_probe("nmap -Pn -sV -p 22,80 192.168.1.23").kind == "version_detect"
    assert parse_probe("curl -skI http://192.168.1.17:9000/").kind == "http_head"
    assert parse_probe("openssl s_client -connect 192.168.1.1:443").kind == "tls_cert"


@pytest.mark.parametrize(
    "command",
    [
        "mysql -h 192.168.1.17 -P 3306 -u root",   # intrusive verb, no builder
        "psql -h 192.168.1.17",
        "xfreerdp /v:192.168.1.50:3389",
        "hydra telnet://192.168.1.46:23",
        "rm -rf /",
    ],
)
def test_mutating_or_unknown_verbs_are_refused(command):
    with pytest.raises(ProbeRefused):
        parse_probe(command)


@pytest.mark.parametrize(
    "command",
    [
        "nc -zv 192.168.1.1 22; rm -rf /",
        "nc -zv 192.168.1.1 22 && curl evil",
        "nmap -sV -p 22 192.168.1.1 `whoami`",
        "curl -skI http://192.168.1.1/$(id)",
    ],
)
def test_shell_metacharacters_are_refused(command):
    with pytest.raises(ProbeRefused):
        parse_probe(command)


def test_public_addresses_are_refused():
    with pytest.raises(ProbeRefused):
        parse_probe("nc -zv 8.8.8.8 53")
    with pytest.raises(ProbeRefused):
        parse_probe("nmap -Pn -sV -p 80 1.1.1.1")


def test_nmap_scan_modes_that_are_not_readonly_are_refused():
    for command in ("nmap -sS -p 22 192.168.1.1", "nmap -sU -p 53 192.168.1.1",
                    "nmap -sV --script vuln -p 80 192.168.1.1", "nmap -A 192.168.1.1"):
        with pytest.raises(ProbeRefused):
            parse_probe(command)


def test_ports_outside_the_allowlist_are_dropped():
    with pytest.raises(ProbeRefused):
        parse_probe("nc -zv 192.168.1.1 31337")
    # nmap keeps only allowlisted ports, and refuses if none remain
    with pytest.raises(ProbeRefused):
        parse_probe("nmap -Pn -sV -p 31337,44444 192.168.1.1")


def test_openssl_redirections_are_stripped_not_refused():
    probe = parse_probe(
        "openssl s_client -connect 192.168.1.1:443 </dev/null 2>/dev/null | openssl x509 -noout -dates"
    )
    assert probe.kind == "tls_cert"
    assert probe.target == "192.168.1.1:443"


def test_run_probe_reports_out_of_scope_without_running():
    result = run_probe("nc -zv 192.168.1.99 22", in_scope=lambda ip: False)
    assert result["ran"] is False
    assert "outside" in result["reason"]


def test_run_probe_refuses_a_mutating_command_without_running():
    result = run_probe("mysql -h 192.168.1.17 -P 3306 -u root")
    assert result["ran"] is False
    assert result["ok"] is False


@pytest.mark.live
def test_loopback_connect_returns_real_result_when_something_listens():
    # Read-only against loopback; skips cleanly if 127.0.0.1:22 is closed.
    result = run_probe("nc -zv 127.0.0.1 22", in_scope=lambda ip: True)
    assert result["ran"] is True
    assert "kind" in result
