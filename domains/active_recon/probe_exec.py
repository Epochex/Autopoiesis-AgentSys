"""Execute the non-mutating steps of a verification playbook, for real.

A read-only step describes a probe an operator would run by hand to confirm a
finding — a TCP connect, a version banner, a TLS certificate read. The platform
already prints that command; running it and returning the actual result removes
the copy-paste round trip for exactly the steps that cannot change anything.

The safety model is the whole module, because "run the command the report
printed" is one careless step away from a command-injection service:

  * The command text is NEVER handed to a shell. It is re-parsed into a typed
    action, and only the allowlisted actions below can be built. Anything else
    — a shell metacharacter, an unknown verb, a mutating flag — fails to parse
    and is refused, not executed.
  * Each action is rebuilt from its parsed arguments into a fixed argv the
    executor controls. The bytes that run are ours, not the caller's string.
  * The target must be a private, in-scope address. This tool probes the lab,
    never an arbitrary host on the internet.
  * Every action is read-only by construction: connect-scan, version banner,
    HTTP HEAD, TLS certificate read. None sends credentials or a payload.

The intrusive steps (hydra/mysql/psql/xfreerdp) have no builder here at all, so
there is no path to run them even if a caller asks.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import Callable

# Ports we will probe. Same spirit as the ownership probe's allowlist: a probe
# tool that accepts any port is a port scanner with an API.
PROBE_PORTS = {
    21, 22, 23, 25, 53, 80, 110, 143, 443, 445, 554, 3306, 3389, 5432, 6379,
    8000, 8080, 8123, 9000, 9092, 9200, 10250, 37777,
}
MAX_PORTS = 8
CONNECT_TIMEOUT = 4
RUN_TIMEOUT = 20


class ProbeRefused(ValueError):
    """The step is not something this executor is willing to run."""


def _private_ip(token: str) -> str:
    try:
        address = ipaddress.ip_address(token)
    except ValueError as exc:
        raise ProbeRefused(f"not an IP address: {token!r}") from exc
    if not address.is_private:
        raise ProbeRefused(f"target is not a private address: {token}")
    return str(address)


def _host_port(token: str) -> tuple[str, int]:
    """Parse host:port, or a bare URL's host:port, into (ip, port)."""
    value = token
    for scheme, default in (("https://", 443), ("http://", 80)):
        if value.startswith(scheme):
            rest = value[len(scheme):].split("/", 1)[0]
            host, _, port = rest.partition(":")
            return _private_ip(host), int(port) if port else default
    host, _, port = token.partition(":")
    if not port:
        raise ProbeRefused(f"expected host:port, got {token!r}")
    return _private_ip(host), int(port)


@dataclass
class Probe:
    kind: str
    target: str
    note: str
    # Exactly one of these is set. argv → run an installed tool with a fixed
    # argument vector; builtin → run an in-process socket probe (no dependency
    # on nmap et al., which we will not install just to read a banner).
    argv: list[str] | None = None
    builtin: str | None = None
    ports: tuple[int, ...] = ()


# --- builders: each turns parsed tokens into a fixed argv we control ---------

def _build_nc(tokens: list[str]) -> Probe:
    # nc -zv <ip> <port>   → read-only connect scan
    flags = [t for t in tokens if t.startswith("-")]
    args = [t for t in tokens if not t.startswith("-")]
    if any(flag not in {"-z", "-zv", "-v", "-vz", "-n", "-w"} for flag in flags):
        raise ProbeRefused(f"nc flags not allowed for a read-only probe: {flags}")
    if len(args) < 2:
        raise ProbeRefused("nc needs a host and a port")
    ip = _private_ip(args[0])
    port = int(args[1])
    if port not in PROBE_PORTS:
        raise ProbeRefused(f"port {port} not in the probe allowlist")
    return Probe(
        kind="tcp_connect",
        argv=["nc", "-z", "-v", "-n", "-w", str(CONNECT_TIMEOUT), ip, str(port)],
        target=f"{ip}:{port}",
        note="TCP connect scan; no payload sent",
    )


def _build_nmap(tokens: list[str]) -> Probe:
    # nmap -Pn -sV -p <ports> <ip>   → version detection.
    # Run as an in-process connect + banner read rather than shelling out to
    # nmap: it is the same read-only operation (open the socket, read what the
    # service announces), needs no package installed, and cannot be talked into
    # a SYN/UDP/script scan because those code paths do not exist here.
    if "-sV" not in tokens and "-sT" not in tokens:
        raise ProbeRefused("only -sV / -sT nmap probes are allowed")
    for banned in ("-sS", "-sU", "-O", "--script", "-A", "-sC"):
        if banned in tokens:
            raise ProbeRefused(f"nmap option not allowed: {banned}")
    ports: list[int] = []
    ip = ""
    iterator = iter(tokens)
    for token in iterator:
        if token == "-p":
            ports = [int(p) for p in next(iterator, "").split(",") if p]
        elif not token.startswith("-"):
            ip = token
    ip = _private_ip(ip)
    ports = [p for p in ports if p in PROBE_PORTS][:MAX_PORTS]
    if not ports:
        raise ProbeRefused("no allowlisted ports to probe")
    return Probe(
        kind="version_detect",
        builtin="banner",
        ports=tuple(ports),
        target=ip,
        note="in-process connect + banner read; no SYN/UDP scan, no NSE scripts",
    )


def _build_curl(tokens: list[str]) -> Probe:
    # curl -skI <url>   → HTTP HEAD, headers only
    url = next((t for t in tokens if t.startswith("http")), "")
    if not url:
        raise ProbeRefused("curl probe needs an http(s) URL")
    ip, port = _host_port(url)
    scheme = "https" if url.startswith("https") or port == 443 else "http"
    return Probe(
        kind="http_head",
        argv=["curl", "-skI", "--max-time", str(CONNECT_TIMEOUT), f"{scheme}://{ip}:{port}/"],
        target=f"{ip}:{port}",
        note="HTTP HEAD; headers only, no body, no auth",
    )


def _build_openssl(tokens: list[str]) -> Probe:
    # openssl s_client -connect <ip>:<port> ... → read the served certificate
    if "s_client" not in tokens or "-connect" not in tokens:
        raise ProbeRefused("only openssl s_client -connect is allowed")
    idx = tokens.index("-connect")
    ip, port = _host_port(tokens[idx + 1])
    return Probe(
        kind="tls_cert",
        argv=["openssl", "s_client", "-connect", f"{ip}:{port}", "-servername", ip],
        target=f"{ip}:{port}",
        note="reads the served certificate; sends no application data",
    )


_BUILDERS: dict[str, Callable[[list[str]], Probe]] = {
    "nc": _build_nc,
    "nmap": _build_nmap,
    "curl": _build_curl,
    "openssl": _build_openssl,
}


def parse_probe(command: str) -> Probe:
    """Re-parse a printed read-only command into a typed, allowlisted probe.

    Refuses anything with shell metacharacters or an unknown verb. The only
    exception is the ``openssl … | openssl x509`` idiom, where the certificate
    read is the first stage and the formatting pipe is dropped — we run the
    connect and format the result ourselves.
    """
    head = command.split("|", 1)[0].strip()
    # The published openssl step carries harmless stdin/stderr redirections
    # (`</dev/null 2>/dev/null`) so an operator can paste it as-is. Strip those
    # known-safe redirections before the metacharacter check rather than refuse
    # them — we drive stdin/stderr ourselves when we run it.
    head = re.sub(r"\s*[12]?>\s*/dev/null", "", head)
    head = re.sub(r"\s*<\s*/dev/null", "", head)
    if any(ch in head for ch in ";&`$><\n") or "$(" in head:
        raise ProbeRefused("command contains shell control characters")
    tokens = shlex.split(head)
    if not tokens:
        raise ProbeRefused("empty command")
    verb = tokens[0]
    builder = _BUILDERS.get(verb)
    if builder is None:
        raise ProbeRefused(f"{verb!r} is not a runnable read-only probe")
    return builder(tokens[1:])


def run_probe(command: str, *, in_scope: Callable[[str], bool] | None = None) -> dict:
    """Parse, scope-check, and execute one read-only probe. Never raises.

    ``in_scope`` decides whether an address belongs to a segment the sweep
    covers; the gateway passes the served-segment check so this cannot be aimed
    at an arbitrary private host outside the lab.
    """
    try:
        probe = parse_probe(command)
    except ProbeRefused as exc:
        return {"ok": False, "ran": False, "reason": str(exc)}

    ip = probe.target.split(":", 1)[0]
    if in_scope is not None and not in_scope(ip):
        return {"ok": False, "ran": False, "reason": f"{ip} is outside the covered segments"}

    base = {"kind": probe.kind, "target": probe.target, "note": probe.note, "read_only": True}

    if probe.builtin == "banner":
        output = _banner_scan(ip, probe.ports)
        return {"ok": True, "ran": True, "argv": ["<in-process connect+banner>"], "exit_code": 0,
                "output": output, **base}

    assert probe.argv is not None
    if shutil.which(probe.argv[0]) is None:
        return {"ok": False, "ran": False, "reason": f"{probe.argv[0]} is not installed on the console host"}
    try:
        completed = subprocess.run(
            probe.argv, capture_output=True, text=True, timeout=RUN_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "ran": True, "reason": f"probe timed out after {RUN_TIMEOUT}s", **base}
    except OSError as exc:
        return {"ok": False, "ran": True, "reason": str(exc), **base}

    output = (completed.stdout or "") + (completed.stderr or "")
    return {"ok": True, "ran": True, "argv": probe.argv, "exit_code": completed.returncode,
            "output": _summarize(probe.kind, output, target=probe.target), **base}


_BANNER_PROMPT = {
    80: b"HEAD / HTTP/1.0\r\n\r\n",
    8000: b"HEAD / HTTP/1.0\r\n\r\n",
    8080: b"HEAD / HTTP/1.0\r\n\r\n",
    9200: b"HEAD / HTTP/1.0\r\n\r\n",
    8123: b"HEAD / HTTP/1.0\r\n\r\n",
}


def _banner_scan(ip: str, ports: tuple[int, ...]) -> str:
    """Connect to each port and read whatever the service announces. Read-only.

    A bare connect proves the port is open; for services that stay quiet until
    spoken to, a single HTTP HEAD is sent (itself a read-only request). Nothing
    else is written, and no port outside PROBE_PORTS is reachable here.
    """
    rows: list[str] = []
    for port in ports:
        line = f"{port}/tcp"
        sock = socket.socket()
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((ip, port))
        except (OSError, socket.timeout) as exc:
            reason = "filtered/timeout" if isinstance(exc, socket.timeout) else "closed"
            rows.append(f"{line:11s} {reason}")
            sock.close()
            continue
        banner = ""
        try:
            sock.settimeout(2.5)
            prompt = _BANNER_PROMPT.get(port)
            if prompt:
                sock.sendall(prompt)
            raw = sock.recv(256)
            banner = raw.decode("latin-1", "replace").splitlines()[0].strip() if raw else ""
        except (OSError, socket.timeout):
            banner = ""
        finally:
            sock.close()
        rows.append(f"{line:11s} open   {banner}".rstrip())
    return "\n".join(rows) if rows else "(no ports probed)"


def _summarize(kind: str, output: str, *, target: str = "") -> str:
    """Trim probe output to the lines an operator actually reads."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if kind == "http_head":
        headers = [line for line in lines if ":" in line or line.startswith("HTTP")]
        if headers:
            lines = headers
        elif not lines:
            # curl -I against a plaintext service (or a refused connect) prints
            # nothing on success; say what happened instead of a blank box.
            return f"no HTTP response from {target} (connection refused or non-HTTP service)"
    elif kind == "tls_cert":
        lines = [line for line in lines if any(k in line for k in ("notBefore", "notAfter", "subject", "issuer", "CN"))] or lines
    trimmed = lines[:20]
    text = "\n".join(trimmed)
    return text[:2000] if text else "(no output)"
