"""Every test here is an attempt to get something past the allowlist.

The happy path is one test. The rest are the ways a command that looks like a
diagnostic turns into a state change: composition, a mutating subcommand, a
program that only reads until you give it the wrong flag, or a read aimed at
the one path that must stay untouched.
"""

from __future__ import annotations

import pytest

from core.investigate.safe_exec import Refused, check, is_safe, parse, run


# ── composition is refused, not escaped ──────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ip addr show; rm -rf /",
        "ip addr show && systemctl stop nginx",
        "df -h | tee /etc/passwd",
        "ss -tulpn > /tmp/out",
        "echo `whoami`",
        "ip addr $(reboot)",
        "df -h\nrm -rf /",
        "uptime || shutdown now",
    ],
)
def test_shell_composition_is_refused(command):
    with pytest.raises(Refused, match="commands run without a shell"):
        check(command)


def test_refusal_names_the_offending_construct():
    with pytest.raises(Refused) as caught:
        check("ip addr show; id")
    assert ";" in str(caught.value)


# ── the allowlist ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ip -br link show eth2",
        "ip addr show",
        "ip route show default",
        "systemctl is-active netops-ops-console-backend",
        "systemctl --failed --no-legend",
        "journalctl -u netops-collector -n 50 --no-pager",
        "df -h /data",
        "ss -tulpn",
        "free -m",
        "uptime",
        "ping -c 3 -W 2 192.168.1.1",
        "nc -zv 192.168.1.46 23",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
    ],
)
def test_real_diagnostics_are_allowed(command):
    assert is_safe(command), f"{command} should have been allowed"


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", "vim /etc/passwd", "apt install nginx", "bash -c id", "sh", "python3 -c 'import os'",
     "chmod 777 /etc", "dd if=/dev/zero of=/dev/sda", "nft flush ruleset", "iptables -F"],
)
def test_programs_outside_the_allowlist_are_refused(command):
    with pytest.raises(Refused, match="not in the read-only allowlist"):
        check(command)


# ── mutating subcommands of otherwise-allowed programs ───────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ip link set eth2 down",
        "ip addr add 10.0.0.1/24 dev eth2",
        "ip route del default",
        "ip neigh flush dev eth2",
        "systemctl restart netops-collector",
        "systemctl stop nginx",
        "systemctl disable netops-collector",
        "docker rm -f container",
        "kubectl delete pod x",
    ],
)
def test_mutating_subcommands_are_refused(command):
    """`ip` and `systemctl` read and write; only the reading half is allowed."""
    with pytest.raises(Refused):
        check(command)


def test_mutating_verb_is_caught_even_in_a_late_position():
    with pytest.raises(Refused, match="would change state"):
        check("ip -br link eth2 set")


# ── the protected path ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ip -br link show tailscale0",
        "ping -c 1 100.126.102.66",
        "systemctl status tailscaled",
        "journalctl -u tailscaled -n 20 --no-pager",
        "nc -zv 100.64.1.9 22",
    ],
)
def test_tailscale_path_is_excluded_even_for_reads(command):
    with pytest.raises(Refused, match="Tailscale"):
        check(command)


def test_ordinary_addresses_are_not_mistaken_for_the_protected_range():
    assert is_safe("ping -c 1 192.168.1.23")
    assert is_safe("ping -c 1 10.42.0.1")


# ── parsing ──────────────────────────────────────────────────────────────────


def test_empty_command_is_refused():
    with pytest.raises(Refused, match="empty"):
        check("   ")


def test_unbalanced_quotes_are_refused_not_guessed_at():
    with pytest.raises(Refused, match="cannot parse"):
        parse('journalctl -u "netops')


def test_quoted_arguments_survive_parsing():
    assert parse('journalctl -u "netops collector" -n 5') == [
        "journalctl", "-u", "netops collector", "-n", "5"
    ]


# ── running ──────────────────────────────────────────────────────────────────


def test_refused_command_reports_why_and_does_not_run():
    execution = run("rm -rf /")
    assert execution.ran is False
    assert execution.exit_code is None
    assert "allowlist" in (execution.refused_reason or "")


def test_allowed_command_actually_runs_and_captures_output():
    execution = run("uptime")
    assert execution.ran is True
    assert execution.exit_code == 0
    assert execution.output


def test_missing_program_is_reported_rather_than_crashing():
    # `nslookup` is allowlisted but is not installed everywhere.
    execution = run("nslookup example.com")
    assert execution.ran or "not installed" in (execution.refused_reason or "")


def test_nonzero_exit_is_still_a_real_run():
    """A command that fails told us something; that is not a refusal."""
    execution = run("ip -br link show definitely-not-a-nic")
    assert execution.ran is True
    assert execution.exit_code != 0 or execution.output == ""


def test_evidence_shape_carries_the_refusal_reason():
    evidence = run("rm -rf /").as_evidence("ev-1")
    assert evidence["evidence_id"] == "ev-1"
    assert evidence["ok"] is False
    assert evidence["refused"]


def test_output_is_capped_so_one_command_cannot_flood_the_session():
    execution = run("journalctl -n 5000 --no-pager")
    assert len(execution.output) <= 20000


# ── curl is a write primitive if you let it be ───────────────────────────────


def test_curl_cannot_write_a_fetched_body_to_disk():
    """`curl -o PATH` downloads to an arbitrary path — cron.d, a unit file, authorized_keys."""
    for target in ["/etc/cron.d/x", "/tmp/payload", "~/.ssh/authorized_keys", "out.sh"]:
        with pytest.raises(Refused, match="write primitive"):
            check(f"curl -s http://192.168.1.1/x -o {target}")


def test_curl_status_code_idiom_still_works():
    assert is_safe("curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz")


@pytest.mark.parametrize("flag", ["-O", "--remote-name", "-T /etc/passwd", "-d payload",
                                  "--data x", "-X POST", "-F f=@/etc/passwd"])
def test_curl_sending_or_storing_flags_are_refused(flag):
    with pytest.raises(Refused, match="sends or stores data"):
        check(f"curl {flag} http://192.168.1.1/")


@pytest.mark.parametrize("url", ["http://evil.com/x", "https://8.8.8.8/", "http://example.org",
                                 "https://user@attacker.net/path"])
def test_curl_may_not_reach_the_public_internet(url):
    with pytest.raises(Refused, match="private address space"):
        check(f"curl -s {url}")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8026/api/healthz", "http://192.168.1.46",
                                 "http://localhost:8026/", "http://10.42.0.1:9100/metrics"])
def test_curl_may_reach_the_local_network(url):
    assert is_safe(f"curl -s -m 5 {url}")


def test_path_traversal_is_refused():
    with pytest.raises(Refused, match="path traversal"):
        check("systemctl status ../../etc/shadow")
