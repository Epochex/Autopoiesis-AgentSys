"""Capture an L2 identity snapshot for the environment sweep.

This is the sensor that closes ``duplicate_ip_static`` -- the fault class that
DHCP logs structurally cannot see, because a device with a hand-configured
address never speaks DHCP at all.

Deliberately passive. It reads the kernel neighbour table this host has already
built from normal traffic and writes it to a file; it sends no probes, scans
nothing, and logs into no device. That keeps it safe to run on a schedule
against a production segment.

Coverage follows from that choice: the table holds only the neighbours this
host has actually talked to. For whole-segment coverage the gateway's own ARP
table (``get system arp``) is the better source -- capture it to the same file
and the detectors read it identically.

Run:  python3 -m domains.network_rca.collect_arp /path/to/arp-snapshot.txt
Then: export AUTOPOIESIS_ARP_SNAPSHOT_PATH=/path/to/arp-snapshot.txt
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from domains.network_rca.environment import parse_arp_table


def capture_neighbour_table(timeout: float = 10.0) -> str:
    """Read the local kernel neighbour table. Read-only, no packets sent."""
    result = subprocess.run(
        ["ip", "neigh", "show"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def write_snapshot(text: str, destination: Path) -> int:
    """Write the point-in-time table atomically and return its record count.

    Atomic because the gateway reads this file on a timer: a half-written table
    would parse as a segment that lost most of its identities, which reads as a
    much bigger event than "the collector was mid-write".
    """
    records = parse_arp_table(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, destination)
    return len(records)


def append_history(text: str, ledger: Path, *, captured_at: str) -> int:
    """Append this capture to the JSONL history and return its record count.

    The history is what makes a conflict visible. One ARP table names a single
    owner per address, so two devices alternating on one address look normal in
    every individual capture -- the fault only exists in the sequence.
    """
    records = parse_arp_table(text)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"captured_at": captured_at, "records": records}, ensure_ascii=False)
    with ledger.open("a") as handle:
        handle.write(line + "\n")
    return len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="file the point-in-time table is written to")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="JSONL history to append this capture to (enables ownership-drift detection)",
    )
    args = parser.parse_args(argv)

    try:
        text = capture_neighbour_table()
    except (OSError, subprocess.SubprocessError) as error:
        print(f"arp snapshot failed: {error}", file=sys.stderr)
        return 1

    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    count = write_snapshot(text, args.destination)
    message = f"{count} L2 identities -> {args.destination}"
    if args.ledger is not None:
        append_history(text, args.ledger, captured_at=captured_at)
        message += f" (+1 capture -> {args.ledger})"
    print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
