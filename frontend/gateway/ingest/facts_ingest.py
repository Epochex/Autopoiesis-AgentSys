"""FortiGate syslog → ClickHouse facts and security-event ingest.

Feeds the situational-awareness console's HISTORICAL device portraits. The raw
FortiGate logs live on R230 (`/data/fortigate-runtime/input/`): the live file
plus ~2.5 months of rotated `.gz`. This reads them over SSH, parses each traffic
line into a flat fact row, normalizes selected authentication and management-
plane signals into `netops.security_events`, and batch-inserts both streams
into ClickHouse (reachable from node 27 at the netops-core cluster IP).

Two modes:
  backfill  — replay every rotated .gz (oldest→newest) + the current file once.
  security-backfill — replay only system/local signals into security_events.
  live      — `tail -F` the current file, inserting new flows continuously.

The live path currently has one sink: ClickHouse. Redpanda publication is not
implemented here, so the correlator requires a separate producer integration.

Config via env (falls back to `/etc/selfevo-console.env` values):
  R230_SSH, R230_PASS, R230_LOG            — SSH target + live log path
  CLICKHOUSE_URL  (default http://10.43.125.243:8123)
  CLICKHOUSE_USER/PASSWORD/DB              — default user/db: netops/netops
  FACTS_LIVE_FLUSH_SECONDS                  — default 5
  FACTS_STATUS_FILE                         — default /run/netops-facts-ingest/status.json
  SECURITY_EVENTS_PROVENANCE                — real (default), replay, or drill
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _read_env_file(path: str) -> dict[str, str]:
    """Read simple systemd EnvironmentFile assignments without evaluating shell."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    values[key] = value
    except OSError:
        pass
    return values


_ENV_FILE = os.getenv("FACTS_ENV_FILE", "/etc/selfevo-console.env")
_FILE_ENV = _read_env_file(_ENV_FILE)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, _FILE_ENV.get(name, default))


CH_URL = _env("CLICKHOUSE_URL", "http://10.43.125.243:8123")
CH_USER = _env("CLICKHOUSE_USER", "netops")
CH_PASS = _env("CLICKHOUSE_PASSWORD")
CH_DB = _env("CLICKHOUSE_DB", "netops")
CH_TABLE = "facts"
SECURITY_TABLE = "security_events"

R230_SSH = _env("R230_SSH", "root@192.168.1.23")
R230_PASS = _env("R230_PASS")
R230_LOG = _env("R230_LOG", "/data/fortigate-runtime/input/fortigate.log")
R230_DIR = os.path.dirname(R230_LOG)

BATCH = int(_env("FACTS_BATCH", "5000"))
LIVE_FLUSH_SECONDS = float(_env("FACTS_LIVE_FLUSH_SECONDS", "5"))
STATUS_FILE = Path(_env("FACTS_STATUS_FILE", "/run/netops-facts-ingest/status.json"))

# FortiGate syslog is space-separated key=value; values may be "quoted".
_KV = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_COLS = [
    "event_ts", "device_key", "srcip", "dstip", "dstport", "proto", "action",
    "service", "app", "type", "subtype", "srcintf", "dstintf", "dstcountry",
    "srcname", "sentbyte", "rcvdbyte",
]
_SECURITY_COLS = [
    "event_ts", "event_id", "event_type", "severity", "device_name", "device_id",
    "virtual_domain", "srcip", "dstip", "dstport", "username", "method",
    "action", "status", "reason", "logid", "logdesc", "message",
    "provenance", "raw_log",
]
_SECURITY_PROVENANCE = frozenset({"real", "replay", "drill"})
_MANAGEMENT_PORTS = frozenset({22, 23, 80, 443, 541, 3000, 8000, 8080, 8443, 10443})


class RecoverableSSHError(RuntimeError):
    """An SSH source interruption that live mode can reconnect after."""


def _kv(line: str) -> dict[str, str]:
    return {m.group(1): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _KV.finditer(line)}


def _ts(d: dict[str, str]) -> str | None:
    """Prefer nanosecond `eventtime`; fall back to date+time. Returns CH DateTime64 text."""
    et = d.get("eventtime")
    if et and et.isdigit():
        sec = int(et) / 1e9
        return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    dt, tm = d.get("date"), d.get("time")
    if dt and tm:
        return f"{dt} {tm}.000"
    return None


def parse_line(line: str) -> list | None:
    """One FortiGate traffic/voip line → a fact row (list matching _COLS), or None."""
    if 'type="traffic"' not in line and 'subtype="voip"' not in line:
        return None
    d = _kv(line)
    ts = _ts(d)
    src = d.get("srcip")
    if not ts or not src:
        return None
    dst = d.get("dstip") or ""
    port = d.get("dstport") or d.get("dst_port") or "0"
    try:
        port_i = int(port)
        port_i = port_i if 0 <= port_i <= 65535 else 0
    except ValueError:
        port_i = 0
    return [
        ts, src, src, dst, port_i,
        d.get("proto", ""), d.get("action", ""),
        d.get("service", ""), d.get("app", ""),
        d.get("type", "traffic"), d.get("subtype", ""),
        d.get("srcintf", ""), d.get("dstintf", ""), d.get("dstcountry", ""),
        d.get("srcname", ""),
        int(d.get("sentbyte") or 0), int(d.get("rcvdbyte") or 0),
    ]


def _uint16(value: str | None) -> int:
    try:
        parsed = int(value or 0)
    except ValueError:
        return 0
    return parsed if 0 <= parsed <= 65535 else 0


def parse_security_event(line: str, provenance: str = "real") -> list | None:
    """Map one FortiGate line to a normalized security event.

    This function has no I/O or mutable state. Unsupported lines return ``None``.
    ``provenance`` describes the origin of the observation, independently of
    whether a historical file happens to be read by the backfill command.
    """
    provenance = provenance.strip().lower()
    if provenance not in _SECURITY_PROVENANCE:
        raise ValueError(f"unsupported security-event provenance: {provenance!r}")

    d = _kv(line)
    ts = _ts(d)
    if not ts:
        return None

    log_type = d.get("type", "").lower()
    subtype = d.get("subtype", "").lower()
    logdesc = d.get("logdesc", "")
    message = d.get("msg", "")
    action = d.get("action", "")
    status = d.get("status", "")
    reason = d.get("reason", "")
    combined = " ".join((logdesc, message, action, status, reason)).lower()
    event_type: str | None = None

    if log_type == "event" and subtype == "system":
        admin_context = "admin" in combined or "administrator" in combined
        login_context = "login" in combined or action.lower() == "login"
        lockout_context = login_context and (
            "disabled" in combined
            or "lockout" in combined
            or "locked out" in combined
            or reason.lower() == "exceed_limit"
        )
        account_disabled = (
            admin_context
            and ("account" in combined or "user" in combined)
            and "disabled" in combined
            and not login_context
        )
        login_failed = admin_context and "login" in combined and any(
            marker in combined for marker in ("failed", "failure", "invalid")
        )
        if account_disabled:
            event_type = "admin_account_disabled"
        elif lockout_context:
            event_type = "admin_login_lockout"
        elif login_failed:
            event_type = "admin_login_failed"

    dstport = _uint16(d.get("dstport") or d.get("dst_port"))
    if log_type == "traffic" and subtype == "local" and dstport in _MANAGEMENT_PORTS:
        traffic_action = action.lower()
        from_wan = d.get("srcintfrole", "").lower() == "wan"
        if traffic_action in {"deny", "blocked", "block", "reject"}:
            event_type = "management_probe"
        elif from_wan and traffic_action in {"accept", "allow", "pass", "close"}:
            event_type = "management_exposure"

    if event_type is None:
        return None
    raw_log = line.rstrip("\r\n")
    event_id = hashlib.sha256(
        f"{provenance}\0{ts}\0{raw_log}".encode("utf-8", errors="replace")
    ).hexdigest()
    return [
        ts,
        event_id,
        event_type,
        d.get("level", ""),
        d.get("devname", ""),
        d.get("devid", ""),
        d.get("vd", ""),
        d.get("srcip", ""),
        d.get("dstip", ""),
        dstport,
        d.get("user", ""),
        d.get("method", ""),
        action,
        status,
        reason,
        d.get("logid", ""),
        logdesc,
        message,
        provenance,
        raw_log,
    ]


def _tsv(rows: list[list]) -> bytes:
    out = []
    for r in rows:
        out.append("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in r))
    return ("\n".join(out) + "\n").encode("utf-8")


def ch_insert(rows: list[list]) -> None:
    if not rows:
        return
    q = f"INSERT INTO {CH_DB}.{CH_TABLE} ({','.join(_COLS)}) FORMAT TabSeparated"
    url = f"{CH_URL}/?query={urllib.parse.quote(q)}"
    req = urllib.request.Request(
        url,
        data=_tsv(rows),
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS},
        method="POST",
    )
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
            return
        except Exception as exc:
            if attempt == 3:
                raise
            print(
                f"[clickhouse] insert attempt {attempt}/3 failed "
                f"({type(exc).__name__}: {exc}); retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2 * attempt)


def _ch_post(query: str, data: bytes | None = None) -> None:
    url = f"{CH_URL}/?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(
        url,
        data=data if data is not None else b"",
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS},
        method="POST",
    )
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                response.read()
            return
        except Exception as exc:
            if attempt == 3:
                raise
            print(
                f"[clickhouse] query attempt {attempt}/3 failed "
                f"({type(exc).__name__}: {exc}); retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2 * attempt)


def create_security_events_table() -> None:
    """Create the independent security-event sink without modifying existing tables."""
    _ch_post(
        f"""CREATE TABLE IF NOT EXISTS {CH_DB}.{SECURITY_TABLE} (
            event_ts DateTime64(3, 'UTC'),
            event_id String,
            event_type LowCardinality(String),
            severity LowCardinality(String),
            device_name String,
            device_id String,
            virtual_domain String,
            srcip String,
            dstip String,
            dstport UInt16,
            username String,
            method LowCardinality(String),
            action LowCardinality(String),
            status LowCardinality(String),
            reason String,
            logid String,
            logdesc String,
            message String,
            provenance Enum8('real' = 1, 'replay' = 2, 'drill' = 3),
            raw_log String,
            ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
        ) ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(event_ts)
        ORDER BY event_id"""
    )


def ch_insert_security(rows: list[list]) -> None:
    if not rows:
        return
    query = (
        f"INSERT INTO {CH_DB}.{SECURITY_TABLE} ({','.join(_SECURITY_COLS)}) "
        "FORMAT TabSeparated"
    )
    _ch_post(query, _tsv(rows))


def _ssh_cmd(remote_cmd: str) -> list[str]:
    base = ["sshpass", "-e"] if R230_PASS else []
    return base + [
        "ssh", "-n", "-T", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30", R230_SSH, remote_cmd,
    ]


def _ssh_env() -> dict[str, str] | None:
    if not R230_PASS:
        return None
    env = os.environ.copy()
    env["SSHPASS"] = R230_PASS
    return env


def _write_status(**updates) -> None:
    """Atomically update non-secret live health state; failure never stops ingest."""
    state: dict = {}
    try:
        state = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    state.update(updates)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = STATUS_FILE.with_name(f".{STATUS_FILE.name}.{os.getpid()}.tmp")
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, STATUS_FILE)
    except OSError as exc:
        print(
            f"[status] failed to update {STATUS_FILE} ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )
        try:
            tmp.unlink()
        except OSError:
            pass


def _source_position() -> dict[str, str | int]:
    """Return R230 log identity and current EOF byte offset through read-only stat."""
    # Space-separated, not \t: inside the remote single-quoted stat format the
    # backslash-t is passed literally to R230's shell, so it arrives as the two
    # characters "\t", not a tab. Splitting on a real tab then never matched and
    # every probe raised "invalid response" — which is what pinned the live
    # tailer in a permanent reconnect loop, inserting nothing.
    remote_cmd = f"stat -Lc '%d:%i %s %Y' -- {shlex.quote(R230_LOG)}"
    try:
        result = subprocess.run(
            _ssh_cmd(remote_cmd),
            env=_ssh_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoverableSSHError("SSH source probe timed out after 15s") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "no stderr output"
        raise RecoverableSSHError(
            f"SSH source probe exited with status {result.returncode}: {detail}"
        )
    parts = result.stdout.strip().split()
    if len(parts) != 3:
        raise RecoverableSSHError("SSH source probe returned an invalid response")
    return {
        "source_file_id": parts[0],
        "source_offset_bytes": int(parts[1]),
        "source_mtime_epoch": int(parts[2]),
    }


def _stream(
    remote_cmd: str,
    on_row,
    label: str,
    on_process=None,
    *,
    live_mode: bool = False,
    clock=None,
    on_security_rows=None,
    security_provenance: str = "real",
    parse_facts: bool = True,
) -> int:
    """Run a remote command and batch-insert parsed rows.

    A finite backfill returns its inserted-row count. A live stream raises
    RecoverableSSHError if SSH reaches EOF so its caller can reconnect.
    """
    if clock is None:
        clock = time.monotonic
    proc = subprocess.Popen(
        _ssh_cmd(remote_cmd),
        env=_ssh_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        bufsize=1 << 20,
    )
    if on_process is not None:
        on_process(proc)
    batch: list[list] = []
    security_batch: list[list] = []
    n = 0
    t0 = time.time()
    last_flush_at = clock() if live_mode else None

    def flush(pulse_at=None) -> None:
        nonlocal batch, security_batch, n, last_flush_at
        if not batch and not security_batch:
            return
        if batch and on_row is not None:
            on_row(batch)
            n += len(batch)
        if security_batch and on_security_rows is not None:
            on_security_rows(security_batch)
        batch = []
        security_batch = []
        if live_mode:
            last_flush_at = pulse_at if pulse_at is not None else clock()

    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="ignore")
            if parse_facts:
                row = parse_line(line)
                if row is not None:
                    batch.append(row)
            if on_security_rows is not None:
                security_row = parse_security_event(line, security_provenance)
                if security_row is not None:
                    security_batch.append(security_row)
            if not batch and not security_batch:
                continue
            pulse_at = clock() if live_mode else None
            size_due = len(batch) >= BATCH or len(security_batch) >= BATCH
            time_due = live_mode and pulse_at - last_flush_at >= LIVE_FLUSH_SECONDS
            if size_due or time_due:
                flush(pulse_at)
                if n and n % (BATCH * 20) == 0:
                    rate = n / max(1e-3, time.time() - t0)
                    print(f"[{label}] {n:,} facts inserted ({rate:,.0f}/s)", flush=True)
        flush()
    except BaseException:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        proc.stdout.close()
    returncode = proc.wait()
    if live_mode:
        raise RecoverableSSHError(f"SSH live stream ended with status {returncode}")
    if returncode != 0:
        raise RuntimeError(f"SSH stream exited with status {returncode}")
    return n


def backfill() -> None:
    """Replay every rotated .gz (oldest→newest) then the current file, into ClickHouse."""
    create_security_events_table()
    provenance = _env("SECURITY_EVENTS_PROVENANCE", "real")
    ls = subprocess.run(
        _ssh_cmd(f"ls -1tr {shlex.quote(R230_DIR)}/"),
        env=_ssh_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    files = [f.strip() for f in ls.stdout.splitlines() if f.strip()]
    gz = [f for f in files if f.endswith(".gz")]
    plain = [f for f in files if f.endswith(".log")]
    print(f"[backfill] {len(gz)} rotated .gz + {len(plain)} current file", flush=True)
    total = 0
    security_total = 0
    for f in gz + plain:
        path = f"{R230_DIR}/{f}"
        cat = "zcat" if f.endswith(".gz") else "cat"
        # grep on the R230 side to cut transfer to just the lines we ingest
        cmd = (
            f"{cat} {shlex.quote(path)} | "
            "grep -aE 'type=\"traffic\"|subtype=\"voip\"|subtype=\"system\"'"
        )
        t0 = time.time()
        security_file_rows = 0

        def insert_security_backfill(rows: list[list]) -> None:
            nonlocal security_file_rows, security_total
            ch_insert_security(rows)
            security_file_rows += len(rows)
            security_total += len(rows)

        n = _stream(
            cmd,
            ch_insert,
            f"backfill:{f}",
            on_security_rows=insert_security_backfill,
            security_provenance=provenance,
        )
        total += n
        print(
            f"[backfill] {f}: +{n:,} facts, +{security_file_rows:,} security events "
            f"in {time.time()-t0:.0f}s (total {total:,}/{security_total:,})",
            flush=True,
        )
    print(f"[backfill] DONE — {total:,} facts, {security_total:,} security events", flush=True)


def security_backfill() -> None:
    """Replay security signals without writing duplicate rows to ``facts``.

    ``security_events`` uses a stable event id and ``ReplacingMergeTree``, so
    re-running this command is safe. The existing ``facts`` table has no
    equivalent event key; keeping this path independent prevents an operational
    memory deployment from duplicating historical flow observations.
    """
    create_security_events_table()
    provenance = _env("SECURITY_EVENTS_PROVENANCE", "real")
    ls = subprocess.run(
        _ssh_cmd(f"ls -1tr {shlex.quote(R230_DIR)}/"),
        env=_ssh_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    files = [f.strip() for f in ls.stdout.splitlines() if f.strip()]
    inputs = [f for f in files if f.endswith((".gz", ".log"))]
    print(f"[security-backfill] {len(inputs)} source files", flush=True)
    total = 0
    for filename in inputs:
        path = f"{R230_DIR}/{filename}"
        cat = "zcat" if filename.endswith(".gz") else "cat"
        # Authentication events are system logs. Management-plane probes are
        # local traffic. Filtering remotely avoids transferring forward traffic
        # while leaving event classification to parse_security_event().
        cmd = (
            f"{cat} {shlex.quote(path)} | "
            "grep -aE 'subtype=\"system\"|subtype=\"local\"' | "
            "grep -aE 'subtype=\"system\"|srcintfrole=\"wan\"' | "
            "grep -aE 'subtype=\"system\"|dstport=(22|23|80|443|541|3000|8000|8080|8443|10443)( |$)'"
        )
        file_rows = 0

        def insert_security_only(rows: list[list]) -> None:
            nonlocal file_rows, total
            ch_insert_security(rows)
            file_rows += len(rows)
            total += len(rows)

        started = time.time()
        _stream(
            cmd,
            None,
            f"security-backfill:{filename}",
            on_security_rows=insert_security_only,
            security_provenance=provenance,
            parse_facts=False,
        )
        print(
            f"[security-backfill] {filename}: +{file_rows:,} events "
            f"in {time.time()-started:.0f}s (total {total:,})",
            flush=True,
        )
    print(f"[security-backfill] DONE — {total:,} security events", flush=True)


def live() -> None:
    """tail -F the current log; insert new flows continuously."""
    create_security_events_table()
    provenance = _env("SECURITY_EVENTS_PROVENANCE", "real")
    print(f"[live] tailing {R230_LOG}", flush=True)
    cmd = (
        f"tail -n0 -F -- {shlex.quote(R230_LOG)} | "
        "grep --line-buffered -aE 'type=\"traffic\"|subtype=\"voip\"|subtype=\"system\"'"
    )
    started_at = datetime.now(timezone.utc).isoformat()
    rows_since_start = 0
    security_rows_since_start = 0
    reconnect_count = 0
    _write_status(
        mode="live",
        pid=os.getpid(),
        started_at=started_at,
        rows_inserted_since_start=0,
        security_rows_inserted_since_start=0,
        reconnect_count=0,
        ssh_connection="connecting",
        ssh_pid=None,
        last_error_type=None,
    )

    def insert_live(rows: list[list]) -> None:
        nonlocal rows_since_start
        ch_insert(rows)
        rows_since_start += len(rows)
        _write_status(
            rows_inserted_since_start=rows_since_start,
            last_insert_at=datetime.now(timezone.utc).isoformat(),
            last_batch_rows=len(rows),
        )
        print(
            f"[live] inserted {len(rows):,} facts "
            f"(total since start {rows_since_start:,})",
            flush=True,
        )

    def insert_security_live(rows: list[list]) -> None:
        nonlocal security_rows_since_start
        ch_insert_security(rows)
        security_rows_since_start += len(rows)
        _write_status(
            security_rows_inserted_since_start=security_rows_since_start,
            security_last_insert_at=datetime.now(timezone.utc).isoformat(),
            security_last_batch_rows=len(rows),
        )
        print(
            f"[live] inserted {len(rows):,} security events "
            f"(total since start {security_rows_since_start:,})",
            flush=True,
        )

    while True:
        try:
            position = _source_position()

            def stream_started(proc: subprocess.Popen) -> None:
                _write_status(
                    ssh_connection="connected",
                    ssh_pid=proc.pid,
                    last_error_type=None,
                    **position,
                )

            _stream(
                cmd,
                insert_live,
                "live",
                stream_started,
                live_mode=True,
                on_security_rows=insert_security_live,
                security_provenance=provenance,
            )
        except RecoverableSSHError as exc:
            reconnect_count += 1
            _write_status(
                ssh_connection="reconnecting",
                ssh_pid=None,
                reconnect_count=reconnect_count,
                last_error_type=type(exc).__name__,
            )
            print(
                f"[live] SSH source interrupted ({exc}); reconnecting in 5s",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            _write_status(
                ssh_connection="error",
                ssh_pid=None,
                reconnect_count=reconnect_count,
                last_error_type=type(exc).__name__,
            )
            print(
                f"[live] fatal stream error ({type(exc).__name__}: {exc})",
                file=sys.stderr,
                flush=True,
            )
            raise
        time.sleep(5)


def _clickhouse_status() -> dict[str, str | int | None]:
    q = (
        f"SELECT count() AS rows, toString(max(event_ts)) AS latest_event "
        f"FROM {CH_DB}.{CH_TABLE} FORMAT JSONEachRow"
    )
    req = urllib.request.Request(
        f"{CH_URL}/",
        data=q.encode("utf-8"),
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        row = json.loads(response.read().decode("utf-8"))
    return {
        "clickhouse_rows": int(row["rows"]),
        "clickhouse_latest_event": row.get("latest_event") or None,
    }


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def status() -> int:
    """Print collector, SSH source, and ClickHouse freshness without exposing secrets."""
    try:
        state = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    collector_running = _pid_alive(state.get("pid"))
    ssh_pid_running = collector_running and _pid_alive(state.get("ssh_pid"))
    collector_ssh = state.get("ssh_connection", "unknown")
    if collector_ssh == "connected" and not ssh_pid_running:
        collector_ssh = "stale"

    print(f"collector: {'running' if collector_running else 'stopped'} (pid={state.get('pid', '-')})")
    print(f"collector_ssh: {collector_ssh}")
    print(f"last_insert_at: {state.get('last_insert_at', '-')}")
    print(f"rows_inserted_since_start: {state.get('rows_inserted_since_start', 0)}")
    print(
        "security_rows_inserted_since_start: "
        f"{state.get('security_rows_inserted_since_start', 0)}"
    )
    print(f"reconnect_count: {state.get('reconnect_count', 0)}")

    ssh_ok = False
    try:
        position = _source_position()
        ssh_ok = True
        print("ssh_probe: reachable")
        print(
            "source_offset: "
            f"{position['source_file_id']}@{position['source_offset_bytes']} bytes "
            f"(mtime={position['source_mtime_epoch']})"
        )
    except Exception as exc:
        print(f"ssh_probe: failed ({type(exc).__name__})")
        print("source_offset: unavailable")

    ch_ok = False
    try:
        metrics = _clickhouse_status()
        ch_ok = True
        print(f"clickhouse_latest_event: {metrics['clickhouse_latest_event']}")
        print(f"clickhouse_rows: {metrics['clickhouse_rows']}")
    except Exception as exc:
        print(f"clickhouse: failed ({type(exc).__name__})")

    return 0 if collector_running and ssh_pid_running and ssh_ok and ch_ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "mode", nargs="?", choices=["backfill", "security-backfill", "live"]
    )
    ap.add_argument("--status", action="store_true", help="report live ingest and ClickHouse freshness")
    args = ap.parse_args()
    if args.status:
        if args.mode is not None:
            ap.error("--status cannot be combined with a mode")
        sys.exit(status())
    if args.mode is None:
        ap.error("mode is required unless --status is used")
    if args.mode == "backfill":
        backfill()
    elif args.mode == "security-backfill":
        security_backfill()
    else:
        live()
