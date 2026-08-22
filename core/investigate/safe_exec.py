"""The allowlist that decides which diagnostic commands may run unattended.

An agent that can propose shell commands is only as safe as the layer that
decides which ones actually execute. That decision is made here, not by the
model, and not by string-matching for "dangerous" words — a denylist loses to
the first construction nobody thought of.

The rules, in the order they are applied:

1. **The command is parsed, never interpolated.** ``shlex.split`` turns the
   text into an argv list. Anything a shell would treat specially — pipes,
   redirects, ``;``, ``&&``, backticks, ``$(...)`` — is rejected outright
   rather than escaped, and execution never goes through a shell.
2. **argv[0] must be in the allowlist**, which contains only programs that
   read. No editor, no package manager, no ``systemctl start|stop|restart``.
3. **Each program has its own subcommand and flag rules.** ``systemctl`` is
   allowed to ``status``/``is-active``/``show``/``list-units`` and nothing
   else; ``ip`` may ``show`` but never ``set``, ``add`` or ``del``.
4. **The Tailscale path is excluded by name**, at this layer as well as in the
   remediation layer, so a read that would be harmless still cannot be used to
   enumerate the one route in and out.

A refusal returns a reason. Silence would leave the caller guessing whether
the command ran and found nothing, or never ran at all.
"""

from __future__ import annotations

import ipaddress
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from core.safety.tailscale import any_tailscale_target

# Shell metacharacters. Their presence means the caller is trying to compose,
# and composition is exactly what this layer refuses to reason about.
SHELL_METACHARACTERS = ("|", ";", "&", ">", "<", "`", "$(", "\n", "\r", "&&", "||")

# Programs whose argv[0] may run at all.
ALLOWED = frozenset({
    "ip", "systemctl", "journalctl", "ss", "df", "free", "uptime", "hostname",
    "date", "lsblk", "dmesg", "ping", "dig", "nslookup", "arp", "nc", "curl",
    "docker", "kubectl", "cloud-init", "netplan",
})

# Programs that take a verb in their first non-flag position. The verb must be
# one that reads. Programs absent from this table have no verb slot — `df`,
# `uptime`, `ss` only ever observe — and are governed by MUTATING alone.
READ_VERBS: dict[str, frozenset[str]] = {
    "ip": frozenset({"addr", "a", "address", "link", "l", "route", "r", "neigh", "n", "maddr"}),
    "systemctl": frozenset({"status", "is-active", "is-enabled", "is-failed", "show", "cat",
                            "list-units", "list-timers", "list-unit-files", "show-environment"}),
    "docker": frozenset({"ps", "images", "stats", "inspect", "logs", "version", "info"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "version", "explain"}),
    "cloud-init": frozenset({"status", "analyze", "query"}),
    "netplan": frozenset({"get", "status"}),
}

# Flags each program may use. An empty set means flags are unrestricted, which
# is only safe for programs with no writing mode at all.
ALLOWED_FLAGS: dict[str, frozenset[str]] = {
    "ip": frozenset({"-br", "-brief", "-4", "-6", "-s", "-d", "-c", "-j", "-o"}),
    "systemctl": frozenset({"--failed", "--no-pager", "--no-legend", "--value", "-p", "--property",
                            "--all", "-a", "--type", "-t", "--state", "-n", "--lines", "-l"}),
    "journalctl": frozenset({"-u", "--unit", "-n", "--lines", "--no-pager", "--since", "--until",
                             "-p", "--priority", "-x", "-e", "-r", "-k", "-b", "-o", "--output",
                             "-t", "--identifier"}),
    "dmesg": frozenset({"-T", "--level", "-l", "--ctime", "-x", "-H", "-P"}),
    "ping": frozenset({"-c", "-W", "-w", "-n", "-q", "-I", "-i", "-s"}),
    "dig": frozenset({"+short", "+time", "+tries", "+noall", "+answer", "-x", "-t"}),
    "arp": frozenset({"-n", "-a", "-e"}),
    "nc": frozenset({"-z", "-v", "-w", "-zv", "-vz", "-n", "-u", "-4", "-6"}),
    "curl": frozenset({"-s", "-S", "-m", "-o", "-w", "-I", "--head", "-L", "--max-time",
                       "--connect-timeout", "-k", "-H", "-A", "--silent"}),
    "kubectl": frozenset({"-n", "--namespace", "-o", "--output", "--all-namespaces", "-A",
                          "-l", "--selector", "--tail", "-c", "--container"}),
    "docker": frozenset({"-a", "--all", "--format", "--no-trunc", "--tail", "-n"}),
    "netplan": frozenset({"--root-dir"}),
    "cloud-init": frozenset({"--long", "--format", "--wait"}),
}

# Verbs that change state. Checked against every token regardless of position,
# because a program whose verb slot passed can still be handed a writing verb
# further along — `ip -br link eth2 set` reads like a query until it is not.
MUTATING = frozenset({
    "set", "add", "del", "delete", "flush", "replace", "change", "append",
    "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask",
    "kill", "rm", "rmi", "prune", "apply", "edit", "create", "patch", "scale",
    "exec", "attach", "cp", "run", "commit", "push", "pull", "build", "up", "down",
    "try", "clean", "init",
})

# Options that consume the next token as their value. Listed once so both the
# flag check and the verb-slot scan agree on where a value ends.
VALUE_TAKING = frozenset({
    "-p", "--priority", "--property", "-n", "--lines", "-u", "--unit", "-o",
    "--output", "-t", "--type", "--state", "-c", "-l", "-w", "-m", "--max-time",
    "--connect-timeout", "-I", "-i", "-s", "--since", "--until", "-H", "-A",
    "--tail", "--namespace", "--selector", "--container", "--format", "--level",
    "--root-dir", "-x",
})

# Programs where an unbounded run can hang the whole investigation.
DEFAULT_TIMEOUT = 20.0


class Refused(Exception):
    """The command will not run, with the reason a person can act on."""


@dataclass
class Execution:
    command: str
    argv: list[str]
    ran: bool
    output: str
    exit_code: int | None
    refused_reason: str | None = None

    def as_evidence(self, evidence_id: str) -> dict[str, Any]:
        return {
            "evidence_id": evidence_id,
            "command": self.command,
            "output": self.output,
            "ok": self.ran and self.exit_code == 0,
            "exit_code": self.exit_code,
            "refused": None if self.ran else self.refused_reason,
        }


def _touches_tailscale(argv: list[str]) -> bool:
    """Delegates to the shared predicate; see core.safety.tailscale."""
    return any_tailscale_target(argv)


def _is_private_host(host: str) -> bool:
    """Only the loopback and RFC1918 space are reachable from a diagnostic.

    A diagnostic that may fetch an arbitrary public URL is a data-exfiltration
    path, and one that may write what it fetched is worse. Both halves are
    closed: destinations are private, and the only permitted output file is
    /dev/null.
    """
    host = host.strip().strip("[]")
    if host in {"localhost", ""}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _check_program_specific(program: str, argv: list[str]) -> None:
    """Rules that a flag allowlist alone cannot express."""
    if program != "curl":
        return

    # `curl -o PATH` writes a file. It is kept only for the status-code idiom
    # `-o /dev/null -w %{http_code}`, and only ever to /dev/null.
    for index, token in enumerate(argv):
        if token in {"-o", "--output"}:
            target = argv[index + 1] if index + 1 < len(argv) else ""
            if target != "/dev/null":
                raise Refused(
                    "curl -o may only write to /dev/null; writing a fetched body "
                    "to any other path is a write primitive, not a diagnostic"
                )
        if token in {"-O", "--remote-name", "--output-dir", "-T", "--upload-file",
                     "-d", "--data", "--data-binary", "-F", "--form", "-X", "--request"}:
            raise Refused(f"curl {token} sends or stores data; only plain reads are permitted")

    urls = [token for token in argv[1:] if "://" in token or token.startswith("www.")]
    for url in urls:
        remainder = url.split("://", 1)[-1]
        host = remainder.split("/", 1)[0].split("@")[-1].split(":")[0]
        if not _is_private_host(host):
            raise Refused(
                f"{host} is outside the private address space; diagnostics may only "
                "reach the loopback and the local network"
            )


def parse(command: str) -> list[str]:
    """Turn command text into argv, refusing anything a shell would compose."""
    text = command.strip()
    if not text:
        raise Refused("empty command")
    for marker in SHELL_METACHARACTERS:
        if marker in text:
            raise Refused(
                f"contains {marker!r}; commands run without a shell, so pipes, "
                "redirects and chaining are refused rather than escaped"
            )
    try:
        argv = shlex.split(text)
    except ValueError as error:
        raise Refused(f"cannot parse: {error}") from error
    if not argv:
        raise Refused("empty command")
    return argv


def check(command: str) -> list[str]:
    """Validate a command and return its argv. Raises Refused with a reason."""
    argv = parse(command)
    program = argv[0]

    if program not in ALLOWED:
        raise Refused(
            f"{program!r} is not in the read-only allowlist; "
            "only programs that observe are permitted here"
        )

    if _touches_tailscale(argv):
        raise Refused(
            "targets the Tailscale path, which is excluded from automated "
            "commands even for reads"
        )

    # Any writing verb, anywhere in the line. This runs before the verb-slot
    # check so `ip link set …` is refused for the reason that actually matters.
    writing = [token for token in argv[1:] if token in MUTATING]
    if writing:
        raise Refused(f"{program} is read-only here; {sorted(set(writing))} would change state")

    # Path traversal in an argument that looks like a name, not a path.
    if any(".." in token for token in argv[1:]):
        raise Refused("argument contains '..'; path traversal is refused")

    # Rules a flag allowlist cannot express run first, so their more specific
    # reason is what the caller sees; the allowlist below is the backstop.
    _check_program_specific(program, argv)

    permitted_flags = ALLOWED_FLAGS.get(program, frozenset())
    if permitted_flags:
        # An option's *value* can itself look like a flag — `--since -24h`,
        # `-p err`, `-n 40`. Skipping the token after a value-taking option
        # stops the checker rejecting a legitimate argument as an unknown flag.
        skip_next = False
        for token in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if not token.startswith("-") and not token.startswith("+"):
                continue
            head = token.split("=", 1)[0]
            if head not in permitted_flags:
                raise Refused(f"{program} {head!r} is not a permitted flag here")
            skip_next = head in VALUE_TAKING and "=" not in token

    verbs = READ_VERBS.get(program)
    if verbs:
        # The verb is the first token that is neither a flag nor a value
        # belonging to one. Options that take a value are skipped in pairs.
        index = 1
        verb: str | None = None
        while index < len(argv):
            token = argv[index]
            if token.startswith("-") or token.startswith("+"):
                takes_value = token in {"-p", "-n", "-o", "-t", "-u", "-l", "-c", "--type",
                                        "--state", "--property", "--namespace", "--output"}
                index += 2 if takes_value and "=" not in token else 1
                continue
            verb = token
            break
        # A line with no verb at all (`systemctl --failed`) is a listing, which
        # reads. Only a verb that is present and unrecognised is refused.
        if verb is not None and verb not in verbs:
            raise Refused(
                f"{program} {verb!r} is not a read-only subcommand; allowed: {sorted(verbs)}"
            )

    return argv


def run(command: str, timeout: float = DEFAULT_TIMEOUT) -> Execution:
    """Validate and run one diagnostic command, capturing whatever it says."""
    try:
        argv = check(command)
    except Refused as refusal:
        return Execution(command=command, argv=[], ran=False, output="", exit_code=None,
                         refused_reason=str(refusal))

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return Execution(command=command, argv=argv, ran=False, output="", exit_code=None,
                         refused_reason=f"{argv[0]} is not installed on this host")
    except subprocess.TimeoutExpired:
        return Execution(command=command, argv=argv, ran=False, output="", exit_code=None,
                         refused_reason=f"timed out after {timeout:g}s")

    output = (completed.stdout or "") + (completed.stderr or "")
    return Execution(
        command=command,
        argv=argv,
        ran=True,
        output=output.strip()[:20000],
        exit_code=completed.returncode,
    )


def is_safe(command: str) -> bool:
    """Whether this command would be allowed, without running it."""
    try:
        check(command)
    except Refused:
        return False
    return True
