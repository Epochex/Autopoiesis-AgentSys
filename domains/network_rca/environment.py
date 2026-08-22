"""Network environment perception over gateway telemetry.

``incidents.py`` replays *known* incidents from a hand-curated fixture. This
module does the opposite and much harder job: sweep the raw gateway corpus and
say what is wrong with the environment **before** anyone has written the
incident down.

The design rule here is the same one the rest of the domain follows: a detector
may only report what a source actually recorded. So the module is built around
two halves that are equally important.

  findings[]   what the present sources *do* prove, with the measurement and
               the evidence count that produced it.
  coverage[]   what the present sources *cannot* prove, named as a fault class
               with the specific sensor that would close it.

That second half exists because of a concrete failure. The 192.168.1.23 dual-MAC
incident was invisible to this system for a structural reason, not a rule
reason: the gateway's only identity source is its own DHCP server, and the
device squatting on .23 held a **manually configured static address**, so it
never emitted a single DHCP packet. No amount of extra rules over DHCP logs can
see an address whose second claimant never speaks DHCP. Only an L2 identity
source (ARP/neighbour table) can. A perception layer that silently omits that
class is worse than one that declares it blind, so ``coverage`` declares it.

Sources this module understands, all optional and all declared:

  dhcp_ack       FortiOS ``DHCP Ack log`` -- ip/mac/lease/hostname/interface
  dhcp_stats     FortiOS ``DHCP statistics`` -- per-pool total/used
  session_clash  FortiOS ``session clash`` -- colliding session tuples
  admin_auth     FortiOS admin login failed / login disabled
  l3_flow        any event carrying srcip/dstip -- who actually talks
  l2_identity    ARP / neighbour table snapshot (opt-in, see ARP_SNAPSHOT_ENV_VAR)

Run:  python3 -m domains.network_rca.environment
"""
from __future__ import annotations

import json
import os
import re

from core.net.addr import is_private
import statistics
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
DEFAULT_SYSLOG_DIR: Path = _HERE / "fixtures" / "real" / "syslog"

# Opt-in L2 identity snapshot. Same discipline as AUTOPOIESIS_LIVE_SYSLOG_PATH:
# no default, no discovery, no device login from inside this module. A human
# points it at a file containing the output of one of the ARP dumps below.
ARP_SNAPSHOT_ENV_VAR: str = "AUTOPOIESIS_ARP_SNAPSHOT_PATH"

# Accumulated L2 history (JSONL, one capture per line). A *single* ARP table
# names one owner per address, so when two devices alternate on one address the
# snapshot shows whichever won the last exchange -- and looks completely normal.
# Ownership drift is only visible across captures, which is why the history is a
# separate source from the snapshot and why the drift detector needs it.
L2_LEDGER_ENV_VAR: str = "AUTOPOIESIS_L2_LEDGER_PATH"

_KV = re.compile(r'(\w+)=("[^"]*"|\S+)')
_CLASH_SRC = re.compile(r"(\d+\.\d+\.\d+\.\d+):\d+->")

# Detector thresholds, kept here so a finding can quote the rule that fired.
DUPLICATE_IP_WINDOW_SECONDS = 300
CHURN_MIN_ACKS = 4
CHURN_LEASE_FRACTION = 0.05   # renewing inside 5% of the lease is not renewal
POOL_PRESSURE_RATIO = 0.80
CLASH_SOURCE_MIN = 10
BRUTEFORCE_MIN_FAILURES = 20


# --------------------------------------------------------------------------
# address helpers
# --------------------------------------------------------------------------

def _segment_of(ip: str) -> str:
    return ".".join(ip.split(".")[:3]) + ".0/24"


def _is_private(ip: str) -> bool:
    """See core.net.addr; kept as a local name for the many call sites."""
    return is_private(ip)


def _is_structural(ip: str) -> bool:
    """Network / broadcast address of a /24 -- never a host, never a finding."""
    last = ip.rsplit(".", 1)[-1]
    return last in {"0", "255"}


def _normalize_mac(mac: str) -> str:
    return mac.strip().lower()


def _is_locally_administered(mac: str) -> bool:
    """True for randomized / software-assigned MACs (U/L bit set in octet 1).

    Modern phones and laptops rotate these per-SSID for privacy. On a managed
    segment they mean the same thing operationally: the address cannot be tied
    to an inventory record, so nothing about that host is accountable.
    """
    head = _normalize_mac(mac).split(":")[0]
    try:
        return bool(int(head, 16) & 0b10) and not bool(int(head, 16) & 0b1)
    except ValueError:
        return False


# --------------------------------------------------------------------------
# corpus sweep -> normalized observation tables
# --------------------------------------------------------------------------

def _parse_kv(line: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in _KV.findall(line)}


def _event_time(fields: dict[str, str]) -> datetime | None:
    date, time = fields.get("date"), fields.get("time")
    if not date or not time:
        return None
    try:
        return datetime.fromisoformat(f"{date}T{time}")
    except ValueError:
        return None


def sweep_corpus(paths: Iterable[Path] | None = None) -> dict[str, Any]:
    """Read the syslog corpus once and build every table the detectors need.

    One pass over the files; detectors are pure functions over the result, so
    adding a detector never adds a file read.
    """
    if paths is None:
        paths = sorted(DEFAULT_SYSLOG_DIR.glob("*.log")) if DEFAULT_SYSLOG_DIR.is_dir() else []
    paths = [Path(p) for p in paths]

    # dhcp_ack table: (ip, mac) -> observation series
    acks: dict[tuple[str, str], dict[str, Any]] = {}
    lease_seconds: dict[str, int] = {}
    ip_hostname: dict[str, str] = {}
    ip_interface: dict[str, str] = {}
    pools: dict[str, dict[str, Any]] = {}
    clash_sources: Counter[str] = Counter()
    clash_events = 0
    admin_failures: Counter[str] = Counter()
    admin_lockouts: Counter[str] = Counter()
    flow_events: Counter[str] = Counter()
    span: list[datetime] = []
    lines_read = 0
    sources_seen: set[str] = set()

    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", errors="ignore") as handle:
            for line in handle:
                lines_read += 1
                fields = _parse_kv(line)
                if not fields:
                    continue
                logdesc = fields.get("logdesc", "")
                stamp = _event_time(fields)
                if stamp is not None:
                    span.append(stamp)

                if logdesc == "DHCP Ack log":
                    sources_seen.add("dhcp_ack")
                    ip = fields.get("ip", "")
                    mac = _normalize_mac(fields.get("mac", ""))
                    if ip and mac:
                        row = acks.setdefault(
                            (ip, mac),
                            {"ip": ip, "mac": mac, "times": [], "interface": fields.get("interface")},
                        )
                        if stamp is not None:
                            row["times"].append(stamp)
                        try:
                            lease_seconds[ip] = int(fields.get("lease", 0))
                        except ValueError:
                            pass
                        hostname = fields.get("hostname")
                        if hostname and hostname != "N/A":
                            ip_hostname[ip] = hostname
                        if fields.get("interface"):
                            ip_interface[ip] = fields["interface"]

                elif logdesc == "DHCP statistics":
                    sources_seen.add("dhcp_stats")
                    pool = fields.get("interface") or "unknown"
                    try:
                        pools[pool] = {
                            "pool": pool,
                            "total": int(fields.get("total", 0)),
                            "used": int(fields.get("used", 0)),
                        }
                    except ValueError:
                        pass

                elif logdesc == "session clash":
                    sources_seen.add("session_clash")
                    clash_events += 1
                    for match in _CLASH_SRC.finditer(line):
                        if _is_private(match.group(1)):
                            clash_sources[match.group(1)] += 1

                elif logdesc == "Admin login failed":
                    sources_seen.add("admin_auth")
                    admin_failures[fields.get("srcip", "unknown")] += 1

                elif logdesc == "Admin login disabled":
                    sources_seen.add("admin_auth")
                    admin_lockouts[fields.get("srcip", "unknown")] += 1

                for key in ("srcip", "dstip"):
                    value = fields.get(key, "")
                    if value and _is_private(value):
                        sources_seen.add("l3_flow")
                        flow_events[value] += 1

    leased_ips = {ip for ip, _ in acks}
    served_segments = {_segment_of(ip) for ip in leased_ips}

    return {
        "dhcp_ack": acks,
        "lease_seconds": lease_seconds,
        "hostname": ip_hostname,
        "interface": ip_interface,
        "dhcp_pools": pools,
        "clash_sources": clash_sources,
        "clash_events": clash_events,
        "admin_failures": admin_failures,
        "admin_lockouts": admin_lockouts,
        "flow_events": flow_events,
        "leased_ips": leased_ips,
        "served_segments": served_segments,
        "sources_seen": sources_seen,
        "corpus": {
            "files": [p.name for p in paths if p.is_file()],
            "lines_read": lines_read,
            "window_start": min(span).isoformat() if span else None,
            "window_end": max(span).isoformat() if span else None,
        },
    }


# --------------------------------------------------------------------------
# L2 identity: the source that can see a static squatter
# --------------------------------------------------------------------------

_ARP_PATTERNS = (
    # FortiOS `get system arp`      -> 192.168.1.23  0  d4:43:0e:1a:c5:88  internal
    re.compile(r"^\s*(?P<ip>\d+\.\d+\.\d+\.\d+)\s+\d+\s+(?P<mac>[0-9a-fA-F:]{17})\s+(?P<iface>\S+)"),
    # FortiOS `diagnose ip arp list` -> index=... ifname=internal 192.168.1.23 d4:43:...
    re.compile(r"ifname=(?P<iface>\S+)\s+(?P<ip>\d+\.\d+\.\d+\.\d+)\s+(?P<mac>[0-9a-fA-F:]{17})"),
    # Linux `ip neigh`               -> 192.168.1.23 dev eth2 lladdr d4:43:... REACHABLE
    re.compile(r"^(?P<ip>\d+\.\d+\.\d+\.\d+)\s+dev\s+(?P<iface>\S+)\s+lladdr\s+(?P<mac>[0-9a-fA-F:]{17})"),
)


def parse_arp_table(text: str) -> list[dict[str, str]]:
    """Parse an ARP/neighbour dump into ``{ip, mac, interface}`` records.

    Accepts FortiOS ``get system arp``, FortiOS ``diagnose ip arp list`` and
    Linux ``ip neigh`` output, because those are the three dumps an operator
    can actually produce during an incident without installing anything.
    """
    records: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        for pattern in _ARP_PATTERNS:
            match = pattern.search(line)
            if match:
                records.append(
                    {
                        "ip": match.group("ip"),
                        "mac": _normalize_mac(match.group("mac")),
                        "interface": match.group("iface"),
                    }
                )
                break
    return records


def load_arp_snapshot(path: str | Path | None = None) -> list[dict[str, str]]:
    """Load the opt-in L2 identity snapshot, or return an empty table.

    Empty is a legitimate answer and must stay distinguishable from "clean":
    ``sensor_coverage`` turns an empty table into a declared blind spot rather
    than into a passing check.
    """
    resolved = path if path is not None else os.environ.get(ARP_SNAPSHOT_ENV_VAR)
    if not resolved:
        return []
    candidate = Path(resolved)
    if not candidate.is_file():
        return []
    return parse_arp_table(candidate.read_text(errors="ignore"))


# An ARP table is a *moment*. A snapshot taken before a device was renumbered
# still shows the old owner, and reporting that as a live conflict would send
# someone chasing a fault that no longer exists. So the age travels with the
# evidence and every finding derived from it says how old it was.
ARP_SNAPSHOT_STALE_SECONDS = 900


# --------------------------------------------------------------------------
# full history: the ClickHouse flow store
# --------------------------------------------------------------------------
#
# The committed syslog corpus is a fixed window. The flow store holds every
# forwarded session the gateway has reported since it was stood up, so it is
# what "all history" actually means here -- and, because it is fed by a live
# pipeline, its newest row is also how the sweep knows whether that pipeline is
# still running. A store that stopped ingesting yesterday is not a live source,
# and reporting it as one is the same mistake as an undeclared blind spot.

_CH_URL = os.getenv("CLICKHOUSE_URL", "")
_CH_USER = os.getenv("CLICKHOUSE_USER", "default")
_CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "")
_CH_DB = os.getenv("CLICKHOUSE_DB", "netops")

# Beyond this, a "live" pipeline is not live; findings that depend on it can no
# longer be re-verified and must say so instead of being silently trusted.
FLOW_STORE_STALE_SECONDS = 3600


def _clickhouse(sql: str, timeout: float = 20.0) -> list[dict[str, Any]] | None:
    """Run one read-only ClickHouse query.

    Never raises, and returns ``None`` for "the query did not run" as distinct
    from ``[]`` for "it ran and matched nothing". Collapsing those two is how a
    failed query turns into "this address went quiet", which would retire a live
    fault on the strength of a timeout.
    """
    if not _CH_URL:
        return None
    url = (
        f"{_CH_URL}/?user={urllib.parse.quote(_CH_USER)}"
        f"&password={urllib.parse.quote(_CH_PASS)}"
    )
    request = urllib.request.Request(
        url, data=(sql + " FORMAT JSON").encode("utf-8"), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")).get("data", [])
    except Exception:
        return None


def _parse_ch_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_flow_store(*, now: datetime | None = None, recent_days: int = 7) -> dict[str, Any]:
    """Summarise the flow store: coverage window, freshness, recent talkers.

    Only aggregates are pulled. The sweep needs three things from 60M+ rows --
    how far back the history goes, whether the pipeline is still writing, and
    which private addresses have carried traffic recently -- and all three are
    single grouped queries.
    """
    span = _clickhouse(
        f"SELECT min(event_ts) AS first, max(event_ts) AS last, count() AS rows "
        f"FROM {_CH_DB}.facts"
    )
    if not span or not span[0].get("last"):
        return {"available": False}

    first = _parse_ch_time(span[0].get("first"))
    last = _parse_ch_time(span[0].get("last"))
    reference = now or datetime.now(timezone.utc)
    age = (reference - last).total_seconds() if last else None

    # The window is anchored to the newest row, not to wall-clock now: when the
    # pipeline has stalled, "the last 7 days" of wall clock can contain no rows
    # at all, and every address would look retired.
    recent = _clickhouse(
        f"SELECT srcip AS ip, count() AS flows, max(event_ts) AS last_seen "
        f"FROM {_CH_DB}.facts "
        f"WHERE event_ts > subtractDays((SELECT max(event_ts) FROM {_CH_DB}.facts), {int(recent_days)}) "
        f"AND (startsWith(srcip, '192.168.') OR startsWith(srcip, '10.') OR startsWith(srcip, '172.')) "
        f"GROUP BY srcip"
    )
    return {
        "available": True,
        "recent_ok": recent is not None,
        "rows": int(span[0].get("rows") or 0),
        "window_start": first.isoformat().replace("+00:00", "Z") if first else None,
        "window_end": last.isoformat().replace("+00:00", "Z") if last else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "flowing": bool(age is not None and age <= FLOW_STORE_STALE_SECONDS),
        "recent_days": recent_days,
        "recent_talkers": {
            str(row["ip"]): int(row["flows"]) for row in (recent or []) if row.get("ip")
        },
    }


def load_l2_history(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the accumulated L2 capture ledger, newest last.

    Each line is one capture: ``{"captured_at": iso, "records": [{ip, mac, ...}]}``.
    Malformed lines are skipped rather than fatal -- a truncated tail from a
    collector killed mid-write must not blind the whole detector.
    """
    resolved = path if path is not None else os.environ.get(L2_LEDGER_ENV_VAR)
    if not resolved:
        return []
    candidate = Path(resolved)
    if not candidate.is_file():
        return []

    captures: list[dict[str, Any]] = []
    for line in candidate.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("records"), list):
            continue
        captures.append(
            {
                "captured_at": str(entry.get("captured_at") or ""),
                "records": [
                    {
                        "ip": str(record.get("ip", "")),
                        "mac": _normalize_mac(str(record.get("mac", ""))),
                        "interface": str(record.get("interface", "")),
                    }
                    for record in entry["records"]
                    if isinstance(record, dict) and record.get("ip") and record.get("mac")
                ],
            }
        )
    captures.sort(key=lambda capture: capture["captured_at"])
    return captures


def detect_l2_ownership_drift(
    observations: dict[str, Any], history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """An address whose L2 owner changed between captures.

    This is the detector the 192.168.1.23 fault actually needed. Two devices
    claiming one address do not both appear in the neighbour table -- they take
    turns, and each individual capture looks healthy. Only the sequence shows
    the address changing hands, and each handover is one interval during which
    return traffic went to the wrong device.
    """
    if len(history) < 2:
        return []

    owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for capture in history:
        for record in capture["records"]:
            series = owners[record["ip"]]
            if not series or series[-1][1] != record["mac"]:
                series.append((capture["captured_at"], record["mac"]))

    leased_macs: dict[str, set[str]] = defaultdict(set)
    for (ip, mac) in observations["dhcp_ack"]:
        leased_macs[ip].add(mac)

    findings: list[dict[str, Any]] = []
    for ip in sorted(owners):
        transitions = owners[ip]
        macs = sorted({mac for _, mac in transitions})
        if len(macs) < 2:
            continue
        leased = sorted(leased_macs.get(ip, set()))
        unaccounted = [mac for mac in macs if mac not in leased]
        findings.append(
            _finding(
                detector="l2_ownership_drift",
                fault_class="duplicate_ip_static",
                subject=ip,
                subject_kind="address",
                severity="critical",
                confidence=0.97,
                headline=(
                    f"{ip} changed L2 owner {len(transitions) - 1}x across "
                    f"{len(history)} captures, between {len(macs)} MACs"
                ),
                measured={
                    "macs": macs,
                    "handovers": len(transitions) - 1,
                    "captures": len(history),
                    "dhcp_leased_macs": leased,
                    "not_leased_by_this_server": unaccounted,
                    # The full handover series, not a sample: it is the only
                    # readable picture of the fault, and a truncated one would
                    # understate how often the address changes hands.
                    "transitions": [
                        {"captured_at": stamp, "mac": mac} for stamp, mac in transitions
                    ][:200],
                    "transitions_truncated": max(0, len(transitions) - 200),
                    "first_capture": history[0]["captured_at"],
                    "last_capture": history[-1]["captured_at"],
                },
                evidence={"source": "l2_identity_history", "captures": len(history)},
                explains=[
                    "intermittent_reachability",
                    "dns_reply_lost",
                    "ssh_session_stall",
                    "relay_reconnect_loop",
                    "return_traffic_misdelivery",
                ],
                cannot_prove=[
                    "which switch port each MAC sits behind -- needs the switch MAC table",
                    "whether one claimant is a legitimate second interface of the same host",
                ],
                next_probe=(
                    f"diagnose switch mac-address list | grep -E '{ '|'.join(macs) }'"
                    if macs
                    else "diagnose switch mac-address list"
                ),
            )
        )
    return findings


def arp_snapshot_provenance(
    path: str | Path | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Where the L2 snapshot came from, when it was captured, and how old it is."""
    resolved = path if path is not None else os.environ.get(ARP_SNAPSHOT_ENV_VAR)
    if not resolved or not Path(resolved).is_file():
        return {"path": str(resolved) if resolved else None, "captured_at": None, "age_seconds": None, "stale": None}
    candidate = Path(resolved)
    captured = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    age = max(0.0, (reference - captured).total_seconds())
    return {
        "path": str(candidate),
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "age_seconds": round(age, 1),
        "stale": age > ARP_SNAPSHOT_STALE_SECONDS,
    }


# --------------------------------------------------------------------------
# finding construction
# --------------------------------------------------------------------------

def _finding(
    *,
    detector: str,
    fault_class: str,
    subject: str,
    subject_kind: str,
    severity: str,
    confidence: float,
    headline: str,
    measured: dict[str, Any],
    evidence: dict[str, Any],
    explains: list[str],
    cannot_prove: list[str],
    next_probe: str,
    source_kinds: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": f"env-{detector}-{subject}".replace("/", "_"),
        "detector": detector,
        "fault_class": fault_class,
        "subject": subject,
        "subject_kind": subject_kind,
        "segment": _segment_of(subject) if subject_kind == "address" else None,
        "severity": severity,
        "confidence": round(confidence, 2),
        "headline": headline,
        "measured": measured,
        "evidence": evidence,
        "explains": explains,
        "cannot_prove": cannot_prove,
        "next_probe": next_probe,
        "source_kinds": source_kinds or ["observed"],
        "current_online_observation": False,
    }


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------

def detect_dhcp_duplicate_ip(
    observations: dict[str, Any], window_seconds: int = DUPLICATE_IP_WINDOW_SECONDS
) -> list[dict[str, Any]]:
    """Two MACs leased the same address inside one window.

    Only sees conflicts where **both** claimants use DHCP. The static-squatter
    case -- the one that actually happened on .23 -- is structurally out of
    reach here; ``detect_identity_contradiction`` is the detector for that.
    """
    by_ip: dict[str, list[tuple[str, list[datetime]]]] = defaultdict(list)
    for (ip, mac), row in observations["dhcp_ack"].items():
        by_ip[ip].append((mac, sorted(row["times"])))

    findings: list[dict[str, Any]] = []
    for ip, claims in sorted(by_ip.items()):
        if len(claims) < 2:
            continue
        overlapping = []
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                a_mac, a_times = claims[i]
                b_mac, b_times = claims[j]
                if not a_times or not b_times:
                    continue
                gap = min(
                    abs((a - b).total_seconds())
                    for a in (a_times[0], a_times[-1])
                    for b in (b_times[0], b_times[-1])
                )
                if gap <= window_seconds:
                    overlapping.append((a_mac, b_mac, gap))
        if not overlapping:
            continue
        macs = sorted({mac for mac, _ in claims})
        findings.append(
            _finding(
                detector="dhcp_duplicate_ip",
                fault_class="duplicate_ip_dhcp",
                subject=ip,
                subject_kind="address",
                severity="critical",
                confidence=0.95,
                headline=f"{ip} leased to {len(macs)} MACs inside {window_seconds}s",
                measured={
                    "mac_count": len(macs),
                    "macs": macs,
                    "window_seconds": window_seconds,
                    "closest_gap_seconds": min(gap for _, _, gap in overlapping),
                },
                evidence={
                    "source": "dhcp_ack",
                    "ack_events": sum(len(times) for _, times in claims),
                },
                explains=["intermittent_reachability", "arp_ownership_drift", "session_reset"],
                cannot_prove=["which claimant is the intended owner"],
                next_probe=f"get system arp | grep {ip}",
            )
        )
    return findings


def detect_identity_contradiction(
    observations: dict[str, Any],
    arp_records: list[dict[str, str]],
    provenance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """An address whose L2 owner is not the MAC the DHCP server leased it to.

    This is the .23 signature and the whole reason the L2 source exists. A
    single ARP snapshot is enough: if the neighbour table answers with a MAC
    the DHCP server never issued for that address, two devices are claiming it
    and only one of them is accountable to the lease database.
    """
    if not arp_records:
        return []

    leased_macs: dict[str, set[str]] = defaultdict(set)
    for (ip, mac) in observations["dhcp_ack"]:
        leased_macs[ip].add(mac)

    # Normalize here as well as in the parser: an ARP table handed in from some
    # other collector may well be uppercase, and a case mismatch would read as
    # "the L2 owner is not the lease holder" -- a false critical on a healthy
    # address, which is the most expensive kind of wrong this module can be.
    arp_macs: dict[str, set[str]] = defaultdict(set)
    arp_iface: dict[str, str] = {}
    for record in arp_records:
        arp_macs[record["ip"]].add(_normalize_mac(record["mac"]))
        arp_iface[record["ip"]] = record.get("interface", "")

    prov = provenance or {}
    snapshot = {
        "snapshot_captured_at": prov.get("captured_at"),
        "snapshot_age_seconds": prov.get("age_seconds"),
    }
    stale_note = (
        [
            f"that the conflict is still live -- the L2 snapshot is "
            f"{prov.get('age_seconds')}s old, older than the {ARP_SNAPSHOT_STALE_SECONDS}s "
            f"freshness bound; re-capture before dispatching anyone"
        ]
        if prov.get("stale")
        else []
    )

    findings: list[dict[str, Any]] = []
    for ip in sorted(arp_macs):
        observed = arp_macs[ip]
        leased = leased_macs.get(ip, set())

        if len(observed) > 1:
            findings.append(
                _finding(
                    detector="identity_contradiction",
                    fault_class="duplicate_ip_static",
                    subject=ip,
                    subject_kind="address",
                    severity="critical",
                    confidence=0.98,
                    headline=f"{ip} answered by {len(observed)} distinct MACs at L2",
                    measured={
                        "arp_macs": sorted(observed),
                        "dhcp_leased_macs": sorted(leased),
                        "interface": arp_iface.get(ip, ""),
                        **snapshot,
                    },
                    evidence={"source": "l2_identity", "arp_records": len(observed)},
                    explains=[
                        "intermittent_reachability",
                        "dns_reply_lost",
                        "ssh_session_stall",
                        "relay_reconnect_loop",
                    ],
                    cannot_prove=[
                        "which MAC is the intended owner without an inventory record",
                        *stale_note,
                    ],
                    next_probe=f"trace both MACs to switch ports, then move the unmanaged one off {ip}",
                    source_kinds=["observed"],
                )
            )
            continue

        if leased and observed and not (observed & leased):
            intruder = sorted(observed)[0]
            findings.append(
                _finding(
                    detector="identity_contradiction",
                    fault_class="duplicate_ip_static",
                    subject=ip,
                    subject_kind="address",
                    severity="critical",
                    confidence=0.9,
                    headline=f"{ip} answers as {intruder} but is leased to {sorted(leased)[0]}",
                    measured={
                        "arp_macs": sorted(observed),
                        "dhcp_leased_macs": sorted(leased),
                        "interface": arp_iface.get(ip, ""),
                        **snapshot,
                    },
                    evidence={"source": "l2_identity+dhcp_ack", "arp_records": len(observed)},
                    explains=["intermittent_reachability", "arp_ownership_drift", "return_traffic_misdelivery"],
                    cannot_prove=[
                        "whether the lease holder is currently powered on",
                        *stale_note,
                    ],
                    next_probe=f"arping -D {ip} from the lease holder, then compare responder MAC",
                )
            )
    return findings


def detect_unmanaged_address(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """Addresses that carry traffic but were never leased by this DHCP server.

    Nothing here is broken *yet*. This is the population that produced the .23
    incident: an address inside a segment the DHCP server hands out from, held
    by a device the DHCP server has no MAC binding for. The server is free to
    lease that same address to someone else, and when it does, neither device
    is at fault and neither is visible as the cause.
    """
    served = observations["served_segments"]
    leased = observations["leased_ips"]
    findings: list[dict[str, Any]] = []

    for ip, events in observations["flow_events"].most_common():
        if ip in leased or _is_structural(ip):
            continue
        segment = _segment_of(ip)
        if segment not in served:
            continue
        gateway_like = ip.rsplit(".", 1)[-1] == "1"
        findings.append(
            _finding(
                detector="unmanaged_address",
                fault_class="address_unmanaged",
                subject=ip,
                subject_kind="address",
                severity="low" if gateway_like else "medium",
                confidence=0.85,
                headline=(
                    f"{ip} carries traffic in a DHCP-served segment with no lease record"
                    + (" (gateway address)" if gateway_like else "")
                ),
                measured={
                    "flow_events": events,
                    "segment": segment,
                    "role": "gateway" if gateway_like else "unmanaged_host",
                },
                evidence={"source": "l3_flow+dhcp_ack", "flow_events": events},
                explains=["future_duplicate_ip", "unattributable_traffic"],
                cannot_prove=[
                    "the device's MAC address -- no DHCP binding and no L2 snapshot for it",
                    "whether the address is excluded from the pool on the gateway",
                ],
                next_probe=f"get system arp | grep {ip}   # bind the address to a MAC",
            )
        )
    return findings


def detect_lease_churn(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """Clients re-acking far inside their own lease time.

    A 7-day lease renewed every few seconds is not renewal, it is a client that
    keeps losing and rebuilding its L3 state -- link flap, roaming loop, or a
    supplicant restarting.

    Reported **per segment**, not per host. When most of a segment behaves this
    way the fault is the segment (an AP stack, an uplink, a relay), and emitting
    one finding per host would bury that under its own symptoms. The ranked host
    list rides along in ``measured.worst`` so triage keeps the detail.
    """
    per_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (ip, mac), row in observations["dhcp_ack"].items():
        times = sorted(row["times"])
        if len(times) < CHURN_MIN_ACKS:
            continue
        lease = observations["lease_seconds"].get(ip, 0)
        if lease <= 0:
            continue
        intervals = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        median = statistics.median(intervals)
        if median >= lease * CHURN_LEASE_FRACTION:
            continue
        per_segment[_segment_of(ip)].append(
            {
                "ip": ip,
                "mac": mac,
                "hostname": observations["hostname"].get(ip),
                "ack_count": len(times),
                "median_interval_seconds": round(median, 1),
                "lease_seconds": lease,
            }
        )

    findings: list[dict[str, Any]] = []
    for segment, hosts in sorted(per_segment.items()):
        leased_here = sum(1 for ip in observations["leased_ips"] if _segment_of(ip) == segment)
        share = len(hosts) / leased_here if leased_here else 0.0
        hosts.sort(key=lambda host: host["ack_count"], reverse=True)
        median_of_medians = statistics.median([host["median_interval_seconds"] for host in hosts])
        lease_seconds = statistics.median([host["lease_seconds"] for host in hosts])
        findings.append(
            _finding(
                detector="lease_churn",
                fault_class="lease_churn",
                subject=segment,
                subject_kind="segment",
                severity="high" if share >= 0.5 else "medium",
                confidence=0.8,
                headline=(
                    f"{segment}: {len(hosts)} of {leased_here} leased hosts re-ack at a "
                    f"{median_of_medians:.0f}s median against a {lease_seconds:.0f}s lease"
                ),
                measured={
                    "churning_hosts": len(hosts),
                    "leased_hosts": leased_here,
                    "share": round(share, 3),
                    "median_interval_seconds": round(median_of_medians, 1),
                    "lease_seconds": int(lease_seconds),
                    "total_acks": sum(host["ack_count"] for host in hosts),
                    "worst": hosts[:12],
                    "worst_truncated": max(0, len(hosts) - 12),
                },
                evidence={"source": "dhcp_ack", "ack_events": sum(host["ack_count"] for host in hosts)},
                explains=["link_flap", "wifi_roaming_loop", "client_supplicant_restart", "dhcp_relay_loop"],
                cannot_prove=[
                    "whether the clients or the link are restarting -- needs switch port counters",
                ],
                next_probe="diagnose switch mac-address list && check AP re-association counters",
            )
        )
    findings.sort(key=lambda finding: finding["measured"]["churning_hosts"], reverse=True)
    return findings


def detect_host_multi_address(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """One MAC holding several leases at once."""
    by_mac: dict[str, set[str]] = defaultdict(set)
    for (ip, mac) in observations["dhcp_ack"]:
        by_mac[mac].add(ip)

    findings: list[dict[str, Any]] = []
    for mac, ips in sorted(by_mac.items()):
        if len(ips) < 2:
            continue
        segments = sorted({_segment_of(ip) for ip in ips})
        findings.append(
            _finding(
                detector="host_multi_address",
                fault_class="host_multi_address",
                subject=mac,
                subject_kind="host",
                severity="medium" if len(segments) > 1 else "low",
                confidence=0.9,
                headline=f"{mac} holds {len(ips)} addresses across {len(segments)} segment(s)",
                measured={"addresses": sorted(ips), "segments": segments},
                evidence={"source": "dhcp_ack", "ack_events": len(ips)},
                explains=["address_pool_waste", "stale_lease", "segment_bridging"],
                cannot_prove=["whether the host is multi-homed on purpose"],
                next_probe=f"diagnose switch mac-address list | grep {mac}",
            )
        )
    return findings


def detect_unmanaged_identity(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """Locally-administered (randomized) MACs holding leases.

    Reported as one segment-level finding, not one per phone: the operational
    fact is "this segment carries N addresses that cannot be tied to inventory",
    and a per-device list of rotating MACs is noise by construction.
    """
    by_segment: dict[str, list[dict[str, str]]] = defaultdict(list)
    for (ip, mac) in observations["dhcp_ack"]:
        if _is_locally_administered(mac):
            by_segment[_segment_of(ip)].append({"ip": ip, "mac": mac})

    findings: list[dict[str, Any]] = []
    for segment, hosts in sorted(by_segment.items()):
        leased_here = sum(1 for ip in observations["leased_ips"] if _segment_of(ip) == segment)
        findings.append(
            _finding(
                detector="unmanaged_identity",
                fault_class="unmanaged_identity",
                subject=segment,
                subject_kind="segment",
                severity="low",
                confidence=0.95,
                headline=(
                    f"{segment} carries {len(hosts)} randomized-MAC hosts "
                    f"of {leased_here} leased"
                ),
                measured={
                    "randomized_hosts": len(hosts),
                    "leased_hosts": leased_here,
                    "share": round(len(hosts) / leased_here, 3) if leased_here else 0.0,
                    "sample": sorted(hosts, key=lambda h: h["ip"])[:8],
                },
                evidence={"source": "dhcp_ack", "ack_events": len(hosts)},
                explains=["inventory_gap", "unattributable_traffic"],
                cannot_prove=["the real hardware identity behind a rotating MAC"],
                next_probe="require 802.1X or MAC reservation on this segment",
            )
        )
    return findings


def detect_pool_pressure(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """DHCP scope utilisation, and scopes whose whole range is handed out."""
    findings: list[dict[str, Any]] = []
    for pool, row in sorted(observations["dhcp_pools"].items()):
        total, used = row["total"], row["used"]
        if total <= 0:
            continue
        ratio = used / total
        if ratio < POOL_PRESSURE_RATIO:
            continue
        findings.append(
            _finding(
                detector="pool_pressure",
                fault_class="pool_pressure",
                subject=pool,
                subject_kind="pool",
                severity="high",
                confidence=0.9,
                headline=f"DHCP scope {pool} is {ratio:.0%} allocated ({used}/{total})",
                measured={"total": total, "used": used, "utilisation": round(ratio, 3)},
                evidence={"source": "dhcp_stats", "stat_samples": 1},
                explains=["lease_denial", "address_reuse_conflict"],
                cannot_prove=["how many leases are stale versus live"],
                next_probe=f"execute dhcp lease-list {pool}",
            )
        )
    return findings


def detect_session_clash(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """Session tuples claimed by more than one internal source."""
    findings: list[dict[str, Any]] = []
    for ip, count in observations["clash_sources"].most_common():
        if count < CLASH_SOURCE_MIN:
            continue
        findings.append(
            _finding(
                detector="session_clash",
                fault_class="session_tuple_clash",
                subject=ip,
                subject_kind="address",
                severity="medium",
                confidence=0.75,
                headline=f"{ip} involved in {count} colliding session tuples",
                measured={"clash_events": count, "corpus_clash_events": observations["clash_events"]},
                evidence={"source": "session_clash", "clash_events": count},
                explains=["nat_port_reuse", "duplicate_ip", "asymmetric_routing"],
                cannot_prove=[
                    "whether the collision is NAT port reuse or two hosts on one address",
                ],
                next_probe=f"diagnose sys session filter src {ip} && diagnose sys session list",
            )
        )
    return findings


def detect_mgmt_bruteforce(observations: dict[str, Any]) -> list[dict[str, Any]]:
    """Failed administrative logins against the gateway's own management plane.

    Emitted as a single management-plane finding, with the attacking network
    blocks as a measurement. Credential attacks rotate the last octet, so a
    per-source row turns one campaign into hundreds of identical rows -- and
    every one of them resolves with the same single action (restrict trusthost),
    which is the test for whether they are really separate findings.
    """
    failures_by_source = observations["admin_failures"]
    total_failures = sum(failures_by_source.values())
    if total_failures < BRUTEFORCE_MIN_FAILURES:
        return []

    per_block: Counter[str] = Counter()
    sources_per_block: dict[str, set[str]] = defaultdict(set)
    for source, failures in failures_by_source.items():
        block = _segment_of(source)
        per_block[block] += failures
        sources_per_block[block].add(source)

    lockouts = sum(observations["admin_lockouts"].values())
    return [
        _finding(
            detector="mgmt_bruteforce",
            fault_class="mgmt_bruteforce",
            subject="management_plane",
            subject_kind="plane",
            severity="critical",
            confidence=0.95,
            headline=(
                f"{total_failures} failed admin logins from {len(failures_by_source)} addresses "
                f"across {len(per_block)} network blocks"
            ),
            measured={
                "failed_logins": total_failures,
                "distinct_sources": len(failures_by_source),
                "distinct_blocks": len(per_block),
                "lockout_events": lockouts,
                "top_blocks": [
                    {
                        "block": block,
                        "failed_logins": count,
                        "distinct_sources": len(sources_per_block[block]),
                    }
                    for block, count in per_block.most_common(8)
                ],
            },
            evidence={"source": "admin_auth", "auth_events": total_failures + lockouts},
            explains=["credential_stuffing", "management_plane_exposure"],
            cannot_prove=["whether any attempt ever succeeded -- needs successful-login audit"],
            next_probe="restrict trusthost on the admin account, then re-check login audit",
        )
    ]


DETECTORS = (
    detect_dhcp_duplicate_ip,
    detect_unmanaged_address,
    detect_lease_churn,
    detect_host_multi_address,
    detect_unmanaged_identity,
    detect_pool_pressure,
    detect_session_clash,
    detect_mgmt_bruteforce,
)


# --------------------------------------------------------------------------
# sensor coverage: the half that keeps the layer honest
# --------------------------------------------------------------------------

# fault class -> (required sources, what closes it when missing)
FAULT_CLASS_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "duplicate_ip_dhcp": {
        "label": "Duplicate address, both claimants use DHCP",
        "requires": ["dhcp_ack"],
        "closes_with": "already covered by the DHCP server log",
    },
    "duplicate_ip_static": {
        "label": "Duplicate address, one claimant is statically configured",
        "requires": ["l2_identity"],
        "closes_with": (
            "an ARP/neighbour snapshot: `get system arp` on the gateway, or "
            f"`ip neigh` on a host in the segment, pointed at {ARP_SNAPSHOT_ENV_VAR}"
        ),
    },
    "address_unmanaged": {
        "label": "Address in use inside a DHCP scope with no lease binding",
        "requires": ["dhcp_ack", "l3_flow"],
        "closes_with": "already covered",
    },
    "lease_churn": {"label": "Client rebuilding its lease continuously", "requires": ["dhcp_ack"], "closes_with": "already covered"},
    "host_multi_address": {"label": "One host holding several addresses", "requires": ["dhcp_ack"], "closes_with": "already covered"},
    "unmanaged_identity": {"label": "Hosts whose MAC cannot be tied to inventory", "requires": ["dhcp_ack"], "closes_with": "already covered"},
    "pool_pressure": {"label": "DHCP scope near exhaustion", "requires": ["dhcp_stats"], "closes_with": "already covered"},
    "session_tuple_clash": {"label": "Session tuple claimed twice", "requires": ["session_clash"], "closes_with": "already covered"},
    "mgmt_bruteforce": {"label": "Management-plane credential attack", "requires": ["admin_auth"], "closes_with": "already covered"},
    "gateway_reachability": {
        "label": "Host cannot reach its own default gateway",
        "requires": ["host_probe"],
        "closes_with": "a per-host probe agent reporting gateway ping / ARP resolution",
    },
    "resolver_failure": {
        "label": "DNS queries leave the host and are never answered",
        "requires": ["host_probe"],
        "closes_with": "a per-host probe agent reporting resolver latency and timeout counts",
    },
    "l2_loop_macflap": {
        "label": "A MAC flapping between switch ports (loop or duplicated host)",
        "requires": ["switch_mac_table"],
        "closes_with": "periodic `diagnose switch mac-address list` from the FortiSwitch stack",
    },
    "rogue_dhcp": {
        "label": "A second DHCP server answering on the segment",
        "requires": ["client_dhcp_view"],
        "closes_with": "a host-side DHCP probe recording which server ID answered the lease",
    },
    "host_config_drift": {
        "label": "Host network config rewritten on reboot (cloud-init, netplan)",
        "requires": ["host_probe"],
        "closes_with": "a per-host preflight reading persistent netplan/cloud-init state",
    },
}


# The sensor that closes a blind class is the single most actionable line in
# the report, so it cannot be the one line the reader cannot read.
_CLOSES_WITH_ZH: dict[str, str] = {
    'already covered by the DHCP server log':
        'DHCP 服务器日志已覆盖',
    'already covered':
        '已覆盖',
    'an ARP/neighbour snapshot: `get system arp` on the gateway, or `ip neigh` on a host in the segment, pointed at AUTOPOIESIS_ARP_SNAPSHOT_PATH':
        '一份 ARP/neighbour 快照:网关上执行 `get system arp`,或网段内主机执行 `ip neigh`,再指向 AUTOPOIESIS_ARP_SNAPSHOT_PATH',
    'a per-host probe agent reporting gateway ping / ARP resolution':
        '主机侧探针,上报网关 ping 与 ARP 解析结果',
    'a per-host probe agent reporting resolver latency and timeout counts':
        '主机侧探针,上报解析延迟与超时计数',
    'periodic `diagnose switch mac-address list` from the FortiSwitch stack':
        '周期性从 FortiSwitch 拉取 `diagnose switch mac-address list`',
    'a host-side DHCP probe recording which server ID answered the lease':
        '主机侧 DHCP 探针,记录是哪个 server ID 应答了租约',
    'a per-host preflight reading persistent netplan/cloud-init state':
        '主机侧预检,读取 netplan / cloud-init 的持久配置',
}


def sensor_coverage(
    observations: dict[str, Any],
    arp_records: list[dict[str, str]],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Say, per fault class, whether this sweep could have detected it at all.

    A class with no source present is reported as ``blind``. That is the field
    the 192.168.1.23 incident would have shown as blind for weeks, and it is
    the reason this function exists instead of an implicit "no findings".
    """
    present = set(observations["sources_seen"])
    if arp_records or history:
        present.add("l2_identity")

    rows: list[dict[str, Any]] = []
    for fault_class, spec in FAULT_CLASS_REQUIREMENTS.items():
        required = spec["requires"]
        have = [source for source in required if source in present]
        missing = [source for source in required if source not in present]
        if not missing:
            state = "covered"
        elif have:
            state = "partial"
        else:
            state = "blind"
        rows.append(
            {
                "fault_class": fault_class,
                "label": spec["label"],
                "coverage": state,
                "requires": required,
                "present": have,
                "missing": missing,
                "closes_with": spec["closes_with"] if missing else None,
                "closes_with_zh": (
                    _CLOSES_WITH_ZH.get(spec["closes_with"], spec["closes_with"]) if missing else None
                ),
            }
        )
    rows.sort(key=lambda row: ({"blind": 0, "partial": 1, "covered": 2}[row["coverage"]], row["fault_class"]))
    return rows


# --------------------------------------------------------------------------
# address-space occupancy: one cell per address, provenance-coloured
# --------------------------------------------------------------------------

def address_space(
    observations: dict[str, Any],
    arp_records: list[dict[str, str]],
    drifted: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Per served /24, classify every host address by how it is *known*.

    The classification is the point. ``leased`` means the gateway holds a MAC
    binding for it; ``unbound`` means something answers on that address and the
    gateway cannot say what. An operator looking at the segment sees the size of
    the blind region directly instead of being told a count.

    ``drifted`` carries addresses the capture history saw change hands. They are
    marked contested even when the newest snapshot shows a single, legitimate
    owner -- because that is exactly what a contested address looks like at any
    one instant, and grading it healthy is the original failure.
    """
    arp_by_ip: dict[str, set[str]] = defaultdict(set)
    for record in arp_records:
        arp_by_ip[record["ip"]].add(_normalize_mac(record["mac"]))

    leased_macs: dict[str, set[str]] = defaultdict(set)
    for (ip, mac) in observations["dhcp_ack"]:
        leased_macs[ip].add(mac)

    segments: list[dict[str, Any]] = []
    for segment in sorted(observations["served_segments"]):
        prefix = segment.rsplit(".", 1)[0]
        cells: list[dict[str, Any]] = []
        for host in range(1, 255):
            ip = f"{prefix}.{host}"
            macs = leased_macs.get(ip, set())
            arp_macs = arp_by_ip.get(ip, set())
            flows = observations["flow_events"].get(ip, 0)
            if ip in drifted:
                state = "contested"
            elif macs and arp_macs and not (macs & arp_macs):
                state = "contested"
            elif len(arp_macs) > 1 or len(macs) > 1:
                state = "contested"
            elif macs:
                state = "leased"
            elif flows or arp_macs:
                state = "unbound"
            else:
                state = "silent"
            cells.append(
                {
                    "ip": ip,
                    "host": host,
                    "state": state,
                    "mac": sorted(macs)[0] if macs else (sorted(arp_macs)[0] if arp_macs else None),
                    "hostname": observations["hostname"].get(ip),
                    "flow_events": flows,
                    "clash_events": observations["clash_sources"].get(ip, 0),
                }
            )
        counts = Counter(cell["state"] for cell in cells)
        segments.append(
            {
                "segment": segment,
                "interface": next(
                    (
                        observations["interface"][ip]
                        for ip in observations["interface"]
                        if _segment_of(ip) == segment
                    ),
                    None,
                ),
                "cells": cells,
                "counts": dict(counts),
            }
        )
    return segments


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# How far back "still happening" reaches when re-checking a finding against the
# live sources.
LIVE_RECHECK_SECONDS = 3600


def _recent_captures(history: list[dict[str, Any]], *, now: datetime | None, seconds: int) -> list[dict[str, Any]]:
    reference = now or datetime.now(timezone.utc)
    recent: list[dict[str, Any]] = []
    for capture in history:
        stamp = _parse_ch_time(capture["captured_at"].replace("Z", ""))
        if stamp is None:
            continue
        if (reference - stamp).total_seconds() <= seconds:
            recent.append(capture)
    return recent


def _verdict(
    state: str, source: str, note: str, note_zh: str = "", *, checked_at: datetime
) -> dict[str, Any]:
    """One re-check result. Human-facing prose ships in both languages.

    The console renders one language at a time; a verdict that exists only in
    English becomes untranslated body copy on the Chinese page, which is exactly
    the kind of text a reader skips -- and a skipped verdict is a finding whose
    live state nobody read."""
    return {
        "state": state,
        "source": source,
        "note": note,
        "note_zh": note_zh or note,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
    }


def verify_findings(
    findings: list[dict[str, Any]],
    *,
    l2_history: list[dict[str, Any]],
    flow_store: dict[str, Any],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-check every finding against the sources that are still flowing.

    Returns ``(open_findings, resolved_findings)``. A finding is only dropped
    when a live source positively shows the condition is gone -- never because
    it is merely old. The difference matters: "the DHCP corpus ends in June" is
    not evidence that a June fault was fixed, and quietly dropping it would turn
    an unmonitored fault into a clean report, which is the failure this whole
    module exists to prevent. Those come back as ``unverifiable`` with the
    source that would settle them.
    """
    checked_at = now or datetime.now(timezone.utc)
    recent = _recent_captures(l2_history, now=checked_at, seconds=LIVE_RECHECK_SECONDS)

    # current L2 picture: owners per address, and addresses per MAC
    owners: dict[str, list[str]] = defaultdict(list)
    macs_to_ips: dict[str, set[str]] = defaultdict(set)
    for capture in recent:
        for record in capture["records"]:
            series = owners[record["ip"]]
            if not series or series[-1] != record["mac"]:
                series.append(record["mac"])
            macs_to_ips[record["mac"]].add(record["ip"])

    # Only a store that both exists and answered its recency query may retire
    # a finding; either failure leaves the finding standing as unverifiable.
    flow_usable = bool(flow_store.get("available") and flow_store.get("recent_ok"))
    talkers = flow_store.get("recent_talkers", {}) if flow_usable else {}
    flow_days = flow_store.get("recent_days")
    # Name the window the verdict actually rests on. When the pipeline has
    # stalled, "the last 7 days" ends wherever it stopped, and a verdict that
    # hides that is claiming freshness it does not have.
    flow_window = (
        f"last {flow_days}d of the flow store"
        if flow_store.get("flowing")
        else f"last {flow_days}d of the flow store, which stops at {flow_store.get('window_end')}"
    )
    flow_window_zh = (
        f"流量存储最近 {flow_days} 天"
        if flow_store.get("flowing")
        else f"流量存储最近 {flow_days} 天(数据截止于 {flow_store.get('window_end')})"
    )
    window_note = f"L2 captures in the last {LIVE_RECHECK_SECONDS // 60}min: {len(recent)}"

    open_findings: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    for finding in findings:
        subject = finding["subject"]
        fault = finding["fault_class"]
        verdict: dict[str, Any]

        if fault == "duplicate_ip_static":
            series = owners.get(subject, [])
            if len(recent) < 2:
                verdict = _verdict(
                    "unverifiable", "l2_identity_history",
                    "fewer than two captures in the recheck window",
                    "复核窗口内采集次数不足两次",
                    checked_at=checked_at,
                )
            elif not series:
                verdict = _verdict(
                    "unverifiable", "l2_identity_history",
                    "address absent from recent captures -- nothing answered for it",
                    "该地址未出现在最近的采集里 —— 没有任何设备为它应答",
                    checked_at=checked_at,
                )
            elif len(set(series)) > 1:
                verdict = _verdict(
                    "confirmed", "l2_identity_history",
                    f"{len(series) - 1} handovers between {len(set(series))} MACs in the recheck window",
                    f"复核窗口内在 {len(set(series))} 个 MAC 之间换手 {len(series) - 1} 次",
                    checked_at=checked_at,
                )
            else:
                # A stable owner is only a resolution when the owner is the MAC
                # that holds the lease. If the address has settled on the
                # *unaccounted* MAC, the lease holder has lost it outright --
                # which is the fault getting worse, not going away. Retiring the
                # finding here would be the exact failure this module exists to
                # prevent: a live source turning a live fault into a clean report.
                leased = {
                    _normalize_mac(mac)
                    for mac in finding.get("measured", {}).get("dhcp_leased_macs", [])
                }
                owner = series[0]
                if not leased:
                    verdict = _verdict(
                        "unverifiable", "l2_identity_history",
                        f"single owner {owner}, but no lease record says which MAC is entitled to the address",
                        f"持有者稳定为 {owner},但没有租约记录能说明该地址应属于哪个 MAC",
                        checked_at=checked_at,
                    )
                elif owner in leased:
                    verdict = _verdict(
                        "resolved", "l2_identity_history",
                        f"address is back with its lease holder {owner} across {len(recent)} recent captures",
                        f"最近 {len(recent)} 次采集地址已稳定回到租约持有者 {owner}",
                        checked_at=checked_at,
                    )
                else:
                    verdict = _verdict(
                        "confirmed", "l2_identity_history",
                        f"address has settled on {owner}, which holds no lease -- the lease holder "
                        f"{sorted(leased)[0]} has lost it entirely across {len(recent)} recent captures",
                        f"地址已稳定落到无租约的 {owner} 手上 —— 最近 {len(recent)} 次采集里,"
                        f"租约持有者 {sorted(leased)[0]} 完全失去了这个地址",
                        checked_at=checked_at,
                    )

        elif fault == "host_multi_address":
            held = macs_to_ips.get(subject, set())
            if not recent:
                verdict = _verdict(
                    "unverifiable", "l2_identity_history", window_note,
                    f"复核窗口内的 L2 采集次数:{len(recent)}", checked_at=checked_at,
                )
            elif len(held) > 1:
                verdict = _verdict(
                    "confirmed", "l2_identity_history",
                    f"still holds {sorted(held)}", f"仍同时持有 {sorted(held)}", checked_at=checked_at,
                )
            else:
                verdict = _verdict(
                    "resolved", "l2_identity_history",
                    "no longer holds more than one address in the recheck window",
                    "复核窗口内已不再同时持有多个地址",
                    checked_at=checked_at,
                )

        elif fault == "address_unmanaged":
            if not flow_usable:
                verdict = _verdict(
                    "unverifiable", "flow_store",
                    "flow store unreachable or its recency query failed -- cannot tell whether the address is still in use",
                    "流量存储不可达或时效查询失败 —— 无法判断该地址是否仍在使用",
                    checked_at=checked_at,
                )
            elif subject in talkers:
                verdict = _verdict(
                    "confirmed", "flow_store",
                    f"{talkers[subject]} flows in the {flow_window}, still no lease binding",
                    f"在{flow_window_zh}内有 {talkers[subject]} 条流量,且仍无租约绑定",
                    checked_at=checked_at,
                )
            else:
                verdict = _verdict(
                    "resolved", "flow_store",
                    f"no traffic in the {flow_window} -- the address is out of use",
                    f"在{flow_window_zh}内没有任何流量 —— 该地址已不再使用",
                    checked_at=checked_at,
                )

        else:
            verdict = _verdict(
                "unverifiable",
                "gateway_syslog",
                "needs a live DHCP/event feed; the committed corpus is a fixed window",
                "需要实时 DHCP/事件源;committed 语料是一个固定窗口",
                checked_at=checked_at,
            )

        enriched = {**finding, "verification": verdict}
        (resolved if verdict["state"] == "resolved" else open_findings).append(enriched)

    return open_findings, resolved


def source_registry(
    observations: dict[str, Any],
    arp_provenance: dict[str, Any],
    history: list[dict[str, Any]],
    flow_store: dict[str, Any],
    arp_records: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every source this sweep read, with its window and whether it is still writing.

    "Live" is a property of the newest row, not of the plumbing. A pipeline that
    stopped ingesting yesterday still answers queries and still looks connected;
    calling it live would make an unmonitored network read as a monitored one.
    """
    reference = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    def _age(stamp: str | None) -> float | None:
        parsed = _parse_ch_time((stamp or "").replace("Z", ""))
        return round((reference - parsed).total_seconds(), 1) if parsed else None

    corpus = observations["corpus"]
    rows.append(
        {
            "id": "gateway_syslog",
            "label": "FortiGate syslog corpus",
            "kind": "historical",
            "window_start": corpus["window_start"],
            "window_end": corpus["window_end"],
            "age_seconds": _age(corpus["window_end"]),
            "flowing": False,
            "volume": corpus["lines_read"],
            "unit": "lines",
            "note": "fixed committed window; not a feed",
        }
    )

    l2_age = _age(history[-1]["captured_at"]) if history else None
    rows.append(
        {
            "id": "l2_identity_history",
            "label": "L2 identity capture ledger",
            "kind": "live",
            "window_start": history[0]["captured_at"] if history else None,
            "window_end": history[-1]["captured_at"] if history else None,
            "age_seconds": l2_age,
            # The collector runs every 5 minutes; allow two missed runs.
            "flowing": bool(l2_age is not None and l2_age <= 900),
            "volume": len(history),
            "unit": "captures",
            "note": "passive neighbour-table capture, every 5 min",
        }
    )

    rows.append(
        {
            "id": "arp_snapshot",
            "label": "L2 point-in-time table",
            "kind": "live",
            "window_start": arp_provenance["captured_at"],
            "window_end": arp_provenance["captured_at"],
            "age_seconds": arp_provenance["age_seconds"],
            "flowing": bool(arp_provenance["stale"] is False),
            "volume": arp_records,
            "unit": "records",
            "note": "newest capture, kept separately for point-in-time checks",
        }
    )

    if flow_store.get("available"):
        rows.append(
            {
                "id": "flow_store",
                "label": "ClickHouse netops.facts",
                "kind": "live",
                "window_start": flow_store.get("window_start"),
                "window_end": flow_store.get("window_end"),
                "age_seconds": flow_store.get("age_seconds"),
                "flowing": bool(flow_store.get("flowing")),
                "volume": flow_store.get("rows", 0),
                "unit": "flows",
                "note": (
                    "full forwarded-session history"
                    if flow_store.get("flowing")
                    else "INGESTION STALLED -- newest row is older than the freshness bound"
                ),
            }
        )
    else:
        rows.append(
            {
                "id": "flow_store",
                "label": "ClickHouse netops.facts",
                "kind": "live",
                "window_start": None,
                "window_end": None,
                "age_seconds": None,
                "flowing": False,
                "volume": 0,
                "unit": "flows",
                "note": "unreachable or empty",
            }
        )
    return rows


# --------------------------------------------------------------------------
# playbooks: what to run to confirm it, and what to run to fix it
# --------------------------------------------------------------------------

# Steps are typed the same way the pentest page types them, so both pages can
# share one renderer. ``readonly`` is safe to run as-is; ``gated`` changes state
# on production equipment and is shown but never executed by the platform.
_PLAYBOOKS: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "duplicate_ip_static": {
        "verify": [
            ("readonly", "watch which MAC currently owns the address", "ip neigh show {subject}"),
            (
                "readonly",
                "differential port probe: the two claimants expose different services, "
                "so the open-port set changes with the ARP owner",
                "for i in $(seq 30); do printf '%s ' \"$(ip neigh show {subject} | grep -o 'lladdr .*' | cut -d' ' -f2)\"; "
                "nc -z -w1 {subject} 22 && printf '22:open ' || printf '22:shut '; sleep 1; echo; done",
            ),
            ("readonly", "confirm the second claimant holds no lease", "get system dhcp server lease-list"),
        ],
        "fix": [
            ("gated", "find the switch port behind each MAC", "diagnose switch mac-address list"),
            ("gated", "move the unmanaged device to a free, reserved address", "# on the device's own console"),
            ("gated", "reserve the address for the legitimate MAC so DHCP cannot reissue it",
             "config system dhcp server / edit <id> / config reserved-address"),
            ("readonly", "clear the stale binding and re-check", "ip neigh flush dev <iface> && ip neigh show {subject}"),
        ],
    },
    "address_unmanaged": {
        "verify": [
            ("readonly", "does anything answer, and with what MAC", "ip neigh show {subject}"),
            ("readonly", "is it inside the served scope with no lease", "get system dhcp server lease-list"),
        ],
        "fix": [
            ("gated", "either reserve the address to its MAC, or exclude it from the pool",
             "config system dhcp server / edit <id> / config exclude-range"),
        ],
    },
    "lease_churn": {
        "verify": [
            ("readonly", "count re-acks per client over a live window", "diagnose debug application dhcps -1"),
            ("readonly", "check AP re-association and port flap counters", "diagnose switch mac-address list"),
        ],
        "fix": [
            ("gated", "raise the lease time or fix the flapping uplink/AP", "config system dhcp server / set lease-time"),
        ],
    },
    "mgmt_bruteforce": {
        "verify": [
            ("readonly", "list current admin accounts and their source restrictions", "show system admin"),
            ("readonly", "re-read the login audit for successes, not just failures", "execute log display"),
        ],
        "fix": [
            ("gated", "restrict the admin account to trusted sources", "config system admin / edit admin / set trusthost1 <cidr>"),
            ("gated", "take the management plane off the WAN interface", "config system interface / unset allowaccess https http"),
        ],
    },
    "session_tuple_clash": {
        "verify": [
            ("readonly", "inspect the colliding sessions for this source", "diagnose sys session filter src {subject} && diagnose sys session list"),
        ],
        "fix": [("gated", "widen the NAT port pool or split the overloaded IP pool", "config firewall ippool")],
    },
    "pool_pressure": {
        "verify": [("readonly", "read the live lease list for this scope", "get system dhcp server lease-list")],
        "fix": [("gated", "widen the scope or shorten the lease", "config system dhcp server")],
    },
    "host_multi_address": {
        "verify": [("readonly", "which addresses this MAC currently answers for", "ip neigh show | grep {subject}")],
        "fix": [("gated", "release the stale lease if the second address is not intentional", "execute dhcp lease-clear <ip>")],
    },
    "unmanaged_identity": {
        "verify": [("readonly", "list leases whose MAC is locally administered", "get system dhcp server lease-list")],
        "fix": [("gated", "require 802.1X or MAC reservation on the segment", "config switch-controller security-policy 802-1X")],
    },
    "duplicate_ip_dhcp": {
        "verify": [("readonly", "read both claims out of the live lease list", "get system dhcp server lease-list")],
        "fix": [("gated", "reserve the address to the intended MAC", "config system dhcp server / config reserved-address")],
    },
}


_PLAYBOOK_ZH: dict[str, str] = {
    'watch which MAC currently owns the address':
        '查看该地址当前由哪个 MAC 持有',
    'differential port probe: the two claimants expose different services, so the open-port set changes with the ARP owner':
        '差分端口探测:两个占用方开放的服务不同,开放端口集合会随 ARP 归属一起变化',
    'confirm the second claimant holds no lease':
        '确认第二个占用方没有租约',
    'find the switch port behind each MAC':
        '定位每个 MAC 背后的交换机端口',
    'move the unmanaged device to a free, reserved address':
        '把无台账的设备迁到一个空闲且已保留的地址',
    'reserve the address for the legitimate MAC so DHCP cannot reissue it':
        '为合法 MAC 保留该地址,防止 DHCP 再次下发',
    'clear the stale binding and re-check':
        '清除过期绑定后复查',
    'does anything answer, and with what MAC':
        '是否有设备应答,MAC 是什么',
    'is it inside the served scope with no lease':
        '它是否位于作用域内且没有租约',
    'either reserve the address to its MAC, or exclude it from the pool':
        '要么把该地址绑定到其 MAC,要么从地址池中排除',
    'count re-acks per client over a live window':
        '在实时窗口内统计每个客户端的重复 ACK 次数',
    'check AP re-association and port flap counters':
        '检查 AP 重关联与端口翻动计数',
    'raise the lease time or fix the flapping uplink/AP':
        '延长租约时间,或修复抖动的上联/AP',
    'list current admin accounts and their source restrictions':
        '列出当前管理员账号及其来源限制',
    're-read the login audit for successes, not just failures':
        '重新审计登录日志,关注成功而不只是失败',
    'restrict the admin account to trusted sources':
        '把管理员账号限制到可信来源',
    'take the management plane off the WAN interface':
        '把管理面从 WAN 接口上摘掉',
    'inspect the colliding sessions for this source':
        '检查该源地址上发生冲突的会话',
    'widen the NAT port pool or split the overloaded IP pool':
        '扩大 NAT 端口池,或拆分过载的 IP 池',
    'read the live lease list for this scope':
        '读取该作用域的实时租约列表',
    'widen the scope or shorten the lease':
        '扩大作用域或缩短租约',
    'which addresses this MAC currently answers for':
        '该 MAC 当前为哪些地址应答',
    'release the stale lease if the second address is not intentional':
        '若第二个地址并非有意为之,释放过期租约',
    'list leases whose MAC is locally administered':
        '列出 MAC 为本地管理位的租约',
    'require 802.1X or MAC reservation on the segment':
        '在该网段启用 802.1X 或 MAC 保留',
    'read both claims out of the live lease list':
        '从实时租约列表中读出两条认领记录',
    'reserve the address to the intended MAC':
        '把该地址保留给预期的 MAC',
}


# Limits are the most-skipped line on any finding, so they cannot be the one
# line that is not in the reader's language.
_LIMIT_ZH: dict[str, str] = {
    'which claimant is the intended owner':
        '哪一方才是该地址的预期主人',
    'which MAC is the intended owner without an inventory record':
        '在没有台账记录的情况下,哪个 MAC 才是预期主人',
    'whether the lease holder is currently powered on':
        '租约持有方当前是否处于开机状态',
    'which switch port each MAC sits behind -- needs the switch MAC table':
        '每个 MAC 位于哪个交换机端口 —— 需要交换机 MAC 表',
    'whether one claimant is a legitimate second interface of the same host':
        '其中一方是否只是同一台主机的第二块网卡',
    "the device's MAC address -- no DHCP binding and no L2 snapshot for it":
        '该设备的 MAC —— 既无 DHCP 绑定,也没有它的 L2 快照',
    'whether the address is excluded from the pool on the gateway':
        '该地址在网关上是否已被排除出地址池',
    'whether the clients or the link are restarting -- needs switch port counters':
        '重启的是客户端还是链路 —— 需要交换机端口计数',
    'whether the host is multi-homed on purpose':
        '该主机是否是有意配置的多宿主',
    'the real hardware identity behind a rotating MAC':
        '轮换 MAC 背后真实的硬件身份',
    'how many leases are stale versus live':
        '其中多少租约已过期、多少仍然活跃',
    'whether the collision is NAT port reuse or two hosts on one address':
        '冲突源自 NAT 端口复用,还是两台主机共用一个地址',
    'whether any attempt ever succeeded -- needs successful-login audit':
        '是否曾有任何一次尝试成功 —— 需要成功登录审计',
}


def build_playbook(finding: dict[str, Any]) -> dict[str, Any]:
    """Verification and repair steps for one finding, with the subject filled in."""
    spec = _PLAYBOOKS.get(finding["fault_class"])
    if spec is None:
        return {"verify": [], "fix": [], "note": "no playbook defined for this fault class"}

    def _render(steps: list[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [
            {
                "risk": risk,
                "what": what,
                # The console renders one language at a time; an English-only
                # step is body copy a Chinese reader skips, and a skipped step
                # is a playbook nobody runs.
                "what_zh": _PLAYBOOK_ZH.get(what, what),
                "command": command.replace("{subject}", str(finding["subject"])),
            }
            for risk, what, command in steps
        ]

    return {"verify": _render(spec["verify"]), "fix": _render(spec["fix"]), "note": ""}


def build_environment_report(
    paths: Iterable[Path] | None = None,
    *,
    arp_snapshot_path: str | Path | None = None,
    l2_ledger_path: str | Path | None = None,
    flow_store: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full sweep over live + historical sources, re-checked against what is still flowing.

    Findings a live source shows are gone are moved out of ``findings`` into
    ``resolved`` -- removed from the working list, but counted, because a
    disappearing row and a row that was never there look identical otherwise.
    """
    observations = sweep_corpus(paths)
    arp_records = load_arp_snapshot(arp_snapshot_path)
    arp_provenance = arp_snapshot_provenance(arp_snapshot_path)
    history = load_l2_history(l2_ledger_path)
    flows = flow_store if flow_store is not None else load_flow_store(now=now)
    checked_at = now or datetime.now(timezone.utc)

    findings: list[dict[str, Any]] = []
    for detector in DETECTORS:
        findings.extend(detector(observations))
    findings.extend(detect_identity_contradiction(observations, arp_records, arp_provenance))
    drift = detect_l2_ownership_drift(observations, history)
    findings.extend(drift)
    # An address already reported as drifting does not also need the
    # point-in-time contradiction row: same address, same fault, one entry.
    drifted_ips = {finding["subject"] for finding in drift}
    findings = [
        finding
        for finding in findings
        if not (finding["detector"] == "identity_contradiction" and finding["subject"] in drifted_ips)
    ]
    findings, resolved = verify_findings(
        findings, l2_history=history, flow_store=flows, now=checked_at
    )
    for finding in findings:
        finding["playbook"] = build_playbook(finding)
        finding["cannot_prove_zh"] = [
            _LIMIT_ZH.get(limit, limit) for limit in finding["cannot_prove"]
        ]
    findings.sort(
        key=lambda f: (
            0 if f["verification"]["state"] == "confirmed" else 1,
            SEVERITY_ORDER.get(f["severity"], 9),
            -f["confidence"],
        )
    )

    coverage = sensor_coverage(observations, arp_records, history)
    by_class = Counter(f["fault_class"] for f in findings)
    by_severity = Counter(f["severity"] for f in findings)
    by_verification = Counter(f["verification"]["state"] for f in findings)
    sources = source_registry(
        observations, arp_provenance, history, flows, len(arp_records), now=checked_at
    )

    return {
        "schema_version": 2,
        "generated_from": "live_sources+full_history",
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "current_online_observation": any(source["flowing"] for source in sources),
        "corpus": observations["corpus"],
        "sources": sources,
        "flow_store": {key: value for key, value in flows.items() if key != "recent_talkers"},
        "sensors": {
            "present": sorted(
                observations["sources_seen"] | ({"l2_identity"} if arp_records or history else set())
            ),
            "l2_identity_records": len(arp_records),
            "l2_identity_env_var": ARP_SNAPSHOT_ENV_VAR,
            "l2_identity_captured_at": arp_provenance["captured_at"],
            "l2_identity_age_seconds": arp_provenance["age_seconds"],
            "l2_identity_stale": arp_provenance["stale"],
            "l2_history_captures": len(history),
            "l2_history_env_var": L2_LEDGER_ENV_VAR,
            "l2_history_window": [history[0]["captured_at"], history[-1]["captured_at"]] if history else None,
        },
        "totals": {
            "findings": len(findings),
            "by_severity": dict(by_severity),
            "by_fault_class": dict(by_class),
            "by_verification": dict(by_verification),
            "resolved_dropped": len(resolved),
            "blind_classes": sum(1 for row in coverage if row["coverage"] == "blind"),
            "leased_addresses": len(observations["leased_ips"]),
            "served_segments": sorted(observations["served_segments"]),
        },
        "findings": findings,
        "resolved": resolved,
        "coverage": coverage,
        "address_space": address_space(observations, arp_records, drifted_ips),
    }


def main() -> None:  # pragma: no cover - operator entry point
    report = build_environment_report()
    slim = {key: value for key, value in report.items() if key != "address_space"}
    print(json.dumps(slim, indent=2, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    main()
