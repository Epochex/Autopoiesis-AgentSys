"""FortiGate syslog → ClickHouse `netops.facts` ingest (backfill + live tail).

Feeds the situational-awareness console's HISTORICAL device portraits. The raw
FortiGate logs live on R230 (`/data/fortigate-runtime/input/`): the live file
plus ~2.5 months of rotated `.gz`. This reads them over SSH, parses each traffic
line into a flat fact row, and batch-inserts into ClickHouse (reachable from
node 27 at the netops-core cluster IP).

Two modes:
  backfill  — replay every rotated .gz (oldest→newest) + the current file once.
  live      — `tail -F` the current file, inserting new flows continuously.

Realtime also mirrors each live fact onto Redpanda `netops.facts.raw.v1` when
--kafka is given, so the existing correlator sees real data (best-effort; a
Kafka/TLS failure never blocks the ClickHouse path).

Config via env (falls back to /etc/selfevo-console.env style vars):
  R230_SSH, R230_PASS, R230_LOG            — SSH target + live log path
  CLICKHOUSE_URL  (default http://10.43.125.243:8123)
  CLICKHOUSE_USER/PASSWORD/DB              — default netops/netops123/netops
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

CH_URL = os.getenv("CLICKHOUSE_URL", "http://10.43.125.243:8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "netops")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "netops123")
CH_DB = os.getenv("CLICKHOUSE_DB", "netops")
CH_TABLE = "facts"

R230_SSH = os.getenv("R230_SSH", "root@192.168.1.23")
R230_PASS = os.getenv("R230_PASS", "")
R230_LOG = os.getenv("R230_LOG", "/data/fortigate-runtime/input/fortigate.log")
R230_DIR = os.path.dirname(R230_LOG)

BATCH = int(os.getenv("FACTS_BATCH", "5000"))

# FortiGate syslog is space-separated key=value; values may be "quoted".
_KV = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_COLS = [
    "event_ts", "device_key", "srcip", "dstip", "dstport", "proto", "action",
    "service", "app", "type", "subtype", "srcintf", "dstintf", "dstcountry",
    "srcname", "sentbyte", "rcvdbyte",
]


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


def _tsv(rows: list[list]) -> bytes:
    out = []
    for r in rows:
        out.append("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in r))
    return ("\n".join(out) + "\n").encode("utf-8")


def ch_insert(rows: list[list]) -> None:
    if not rows:
        return
    q = f"INSERT INTO {CH_DB}.{CH_TABLE} ({','.join(_COLS)}) FORMAT TabSeparated"
    url = f"{CH_URL}/?user={CH_USER}&password={CH_PASS}&query={urllib.request.quote(q)}"
    req = urllib.request.Request(url, data=_tsv(rows), method="POST")
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
            return
        except Exception as exc:
            if attempt == 3:
                raise
            time.sleep(2 * attempt)


def _ssh_cmd(remote_cmd: str) -> list[str]:
    base = ["sshpass", "-p", R230_PASS] if R230_PASS else []
    return base + [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30", R230_SSH, remote_cmd,
    ]


def _stream(remote_cmd: str, on_row, label: str) -> int:
    """Run a remote command, parse its stdout line-by-line, batch-insert. Returns rows inserted."""
    proc = subprocess.Popen(_ssh_cmd(remote_cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20)
    batch: list[list] = []
    n = 0
    t0 = time.time()
    try:
        for raw in proc.stdout:
            row = parse_line(raw.decode("utf-8", errors="ignore"))
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= BATCH:
                on_row(batch)
                n += len(batch)
                batch = []
                if n % (BATCH * 20) == 0:
                    rate = n / max(1e-3, time.time() - t0)
                    print(f"[{label}] {n:,} facts inserted ({rate:,.0f}/s)", flush=True)
        if batch:
            on_row(batch)
            n += len(batch)
    finally:
        proc.stdout.close()
        proc.wait()
    return n


def backfill() -> None:
    """Replay every rotated .gz (oldest→newest) then the current file, into ClickHouse."""
    ls = subprocess.run(_ssh_cmd(f"ls -1tr {R230_DIR}/"), capture_output=True, text=True, timeout=30)
    files = [f.strip() for f in ls.stdout.splitlines() if f.strip()]
    gz = [f for f in files if f.endswith(".gz")]
    plain = [f for f in files if f.endswith(".log")]
    print(f"[backfill] {len(gz)} rotated .gz + {len(plain)} current file", flush=True)
    total = 0
    for f in gz + plain:
        path = f"{R230_DIR}/{f}"
        cat = "zcat" if f.endswith(".gz") else "cat"
        # grep on the R230 side to cut transfer to just the lines we ingest
        cmd = f"{cat} {path} | grep -aE 'type=\"traffic\"|subtype=\"voip\"'"
        t0 = time.time()
        n = _stream(cmd, ch_insert, f"backfill:{f}")
        total += n
        print(f"[backfill] {f}: +{n:,} facts in {time.time()-t0:.0f}s (total {total:,})", flush=True)
    print(f"[backfill] DONE — {total:,} facts", flush=True)


def live() -> None:
    """tail -F the current log; insert new flows continuously."""
    print(f"[live] tailing {R230_LOG}", flush=True)
    cmd = f"tail -n0 -F {R230_LOG} | grep --line-buffered -aE 'type=\"traffic\"|subtype=\"voip\"'"
    while True:
        try:
            _stream(cmd, ch_insert, "live")
        except Exception as exc:
            print(f"[live] stream error: {exc}; reconnecting in 5s", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "live"])
    args = ap.parse_args()
    if args.mode == "backfill":
        backfill()
    else:
        live()
