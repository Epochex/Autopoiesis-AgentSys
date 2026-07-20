"""Deterministic FortiGate-syslog fault injectors for the RCA detection demo.

Each injector crafts a REALISTIC FortiOS syslog incident sequence for one of the
known network-RCA incident types and returns it as an :class:`InjectedIncident`.
The emitted lines are real-shaped: they clone the field layout of the committed
capture in ``domains/network_rca/fixtures/real/syslog/*.log`` (``logid=``, ``type=``,
``subtype=``, ``action=``, ``srcip=``, ``dstport=``, ``logdesc=`` …).

Why this is a REAL detection, not a scripted puppet show
-------------------------------------------------------
The injector only produces raw syslog *lines* and an operator *symptom* case. The
verdict is NOT baked in. Detection flows through the existing pipeline unchanged:

    injected syslog lines
        -> aggregate_window_stats()          # parse + count the emitted lines
        -> real_window_stats.json (schema)   # same shape the real R230 capture uses
        -> RealSyslogAdapter                  # domains/network_rca/adapters
        -> register_real_rca_skills           # the read-only skill library
        -> SkillAttentionController           # attention gate over the skills
        -> build_diagnosis / _infer_from_evidence   # domains/network_rca/reasoner.py
        -> Verifier                           # core/verifier

``aggregate_window_stats`` DERIVES every statistic by parsing the emitted lines and
counting structured fields — nothing is hand-written into the stats. Change the
injected traffic and the statistics (and therefore the diagnosis) change with it.
The ``expected_root_cause_key`` on each incident is only the assertion target the
harness checks the *real* reasoner against; it is never fed into the reasoner.

The syslog->signal classification here keys on the same structured fields the real
adapter documents in each evidence unit's ``source`` (``logdesc`` exact string /
``action`` / ``dstport`` in the DVR set), matching
``core.eval.fortigate_corpus_retrieval.classify_category``.
"""
from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from domains.network_rca.schema import RCASeedCase


# Dahua camera/DVR SDK service ports. A denied flow to one of these is the
# "device port probe" signal — the same {37777,37809,37810} set documented in
# ``real_syslog_adapter._ev_device_port_probe``'s source filter.
DVR_PORTS: tuple[str, ...] = ("37777", "37809", "37810")

# Non-DVR destination ports seen on the real deny flood (NetBIOS 137 dominates).
_DENY_PORTS: tuple[str, ...] = ("137", "5050", "5600", "48689", "28689")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_BASE = datetime(2026, 7, 16, 0, 0, 0)
_DEVID = 'devname="DAHUA_FORTIGATE" devid="FG100ETK20014183"'


# ── raw-line templating (clones the committed capture's field layout) ───────────
def _prefix(dt: datetime) -> str:
    """BSD-syslog prefix, e.g. ``Jul 16 00:01:31 _gateway date=... time=...``."""
    stamp = f"{_MONTHS[dt.month - 1]} {dt.day:2d} {dt:%H:%M:%S}"
    return f'{stamp} _gateway date={dt:%Y-%m-%d} time={dt:%H:%M:%S} {_DEVID}'


def _eventtime(dt: datetime, salt: int = 0) -> int:
    return int(dt.timestamp() * 1_000_000_000) + (salt % 1_000_000_000)


def _line_admin_fail(dt: datetime, srcip: str, user: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100032002" type="event" subtype="system" level="alert" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="Admin login failed" '
        f'sn="0" user="{user}" ui="https({srcip})" method="https" srcip={srcip} '
        f'dstip=77.236.99.125 action="login" status="failed" reason="name_invalid" '
        f'msg="Administrator {user} login failed from https({srcip}) because of invalid user name"'
    )


def _line_lockout(dt: datetime, srcip: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100032021" type="event" subtype="system" level="alert" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="Admin login disabled" '
        f'ui="{srcip}" action="login" status="failed" reason="exceed_limit" '
        f'msg="Login disabled from IP {srcip} for 60 seconds because of 3 bad attempts"'
    )


def _line_policy_deny(dt: datetime, srcip: str, dstport: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0001000014" type="traffic" subtype="local" level="notice" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" srcip={srcip} srcport={dstport} '
        f'srcintf="port5" srcintfrole="lan" dstip=255.255.255.255 dstport={dstport} '
        f'dstintf="unknown0" dstintfrole="undefined" sessionid={1665174822 + salt} proto=17 '
        f'action="deny" policyid=0 policytype="local-in-policy" service="udp/{dstport}" '
        f'dstcountry="Reserved" srccountry="Reserved" trandisp="noop" app="udp/{dstport}" '
        f'duration=0 sentbyte=0 rcvdbyte=0 sentpkt=0 appcat="unscanned"'
    )


def _line_device_probe(dt: datetime, srcip: str, dstip: str, dvrport: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0000000013" type="traffic" subtype="forward" level="notice" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" srcip={srcip} srcport=53312 '
        f'srcintf="wan1" srcintfrole="wan" dstip={dstip} dstport={dvrport} dstintf="LACP" '
        f'dstintfrole="lan" sessionid={1665174831 + salt} proto=6 action="deny" policyid=0 '
        f'policytype="policy" service="Dahua SDK" dstcountry="Reserved" srccountry="Reserved" '
        f'trandisp="noop" duration=0 sentbyte=0 rcvdbyte=0 sentpkt=0 appcat="unscanned" '
        f'crscore=30 craction=131072 crlevel="high" srcmac="d4:43:0e:1a:c5:88" srcserver=1'
    )


def _line_accept(dt: datetime, srcip: str, dstip: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0001000014" type="traffic" subtype="local" level="notice" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" srcip={srcip} srcport=123 '
        f'srcintf="unknown0" srcintfrole="undefined" dstip={dstip} dstport=123 dstintf="wan1" '
        f'dstintfrole="wan" sessionid={1665172077 + salt} proto=17 action="accept" policyid=0 '
        f'service="NTP" dstcountry="United States" srccountry="France" trandisp="noop" app="NTP" '
        f'duration=180 sentbyte=76 rcvdbyte=76 sentpkt=1 rcvdpkt=1 appcat="unscanned"'
    )


def _line_update(dt: datetime, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100041000" type="event" subtype="system" level="notice" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="FortiGate update succeeded" '
        f'status="update" msg="Fortigate scheduled update fcni=yes fdni=yes fsci=yes from 149.5.232.66:443"'
    )


def _line_dhcp_ack(dt: datetime, ip: str, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100026001" type="event" subtype="system" level="information" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="DHCP Ack log" '
        f'interface="LACP" dhcp_msg="Ack" mac="84:15:D3:DF:E3:86" ip={ip} lease=604800 '
        f'hostname="N/A" msg="DHCP server sends a DHCPACK"'
    )


def _line_dhcp_stats(dt: datetime, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100026003" type="event" subtype="system" level="information" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="DHCP statistics" '
        f'interface="showroom-vlan" total=245 used=0 msg="DHCP statistics"'
    )


def _line_session_clash(dt: datetime, salt: int) -> str:
    tuple_str = (
        f"state=18050200 tuple-num=2 policyid=21 dir=0 act=1 hook=4 "
        f"192.168.1.16:3702->192.168.100.108:56317(77.236.99.122:64118)"
    )
    return (
        f'{_prefix(dt)} logid="0100020085" type="event" subtype="system" level="information" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="session clash" '
        f'status="clash" proto=17 msg="session clash" new_status="{tuple_str}" old_status="{tuple_str}"'
    )


def _line_perf(dt: datetime, cpu: int, mem: int, sess: int, salt: int) -> str:
    return (
        f'{_prefix(dt)} logid="0100040704" type="event" subtype="system" level="notice" '
        f'vd="root" eventtime={_eventtime(dt, salt)} tz="+0200" logdesc="System performance statistics" '
        f'action="perf-stats" cpu={cpu} mem={mem} totalsession={sess} disk=0 bandwidth="401/353" '
        f'setuprate=0 disklograte=0 fazlograte=0 freediskstorage=1409 sysuptime=48232307 '
        f'msg="Performance statistics: average CPU: {cpu}, memory: {mem}, concurrent sessions: {sess}"'
    )


# ── aggregation: emitted syslog lines -> real_window_stats.json schema ──────────
# Fast, total regex parser (mirrors core.eval.fortigate_corpus_retrieval.parse_syslog_line);
# the emitted lines are ALSO consumable by the production shlex parser
# domains/network_rca/adapters/fortios_syslog.parse_fortios_kv_line (asserted in tests).
_KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_line(line: str) -> dict[str, str]:
    """Parse one raw FortiGate ``key=value`` line into a field dict (never raises)."""
    out: dict[str, str] = {}
    for key, val in _KV_RE.findall(line):
        if val and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        out[key] = val
    return out


def _top(counter: Counter, n: int) -> list[list]:
    return [[value, count] for value, count in counter.most_common(n)]


def aggregate_window_stats(lines: list[str], window_days: list[str] | None = None) -> dict:
    """Aggregate raw syslog lines into the ``real_window_stats.json`` schema.

    Every field is COUNTED from the parsed lines; nothing is hand-written. This is
    the genuine syslog->stats step that feeds :class:`RealSyslogAdapter` unchanged.
    """
    admin_fail_src: Counter = Counter()
    lockouts = 0
    deny_ports: Counter = Counter()
    deny_src: Counter = Counter()
    deny_count = 0
    device_ports: Counter = Counter()
    device_deny = 0
    accept_permit = 0
    session_clash = 0
    update_ok = 0
    dhcp_ack = 0
    dhcp_stats = 0
    security_rating = 0
    perf_sample: dict[str, str] | None = None

    for line in lines:
        f = parse_line(line)
        logdesc = f.get("logdesc", "")
        action = f.get("action", "")
        dstport = f.get("dstport", "")
        if logdesc == "Admin login failed":
            if f.get("srcip"):
                admin_fail_src[f["srcip"]] += 1
        elif logdesc == "Admin login disabled":
            lockouts += 1
        elif logdesc == "session clash":
            session_clash += 1
        elif logdesc == "FortiGate update succeeded":
            update_ok += 1
        elif logdesc == "DHCP Ack log":
            dhcp_ack += 1
        elif logdesc == "DHCP statistics":
            dhcp_stats += 1
        elif logdesc == "Security Rating":
            security_rating += 1
        elif logdesc == "System performance statistics":
            if perf_sample is None:
                perf_sample = {
                    "cpu": f.get("cpu", "0"),
                    "mem": f.get("mem", "0"),
                    "totalsession": f.get("totalsession", "0"),
                    "disk": f.get("disk", "0"),
                    "setuprate": f.get("setuprate", "0"),
                }
        elif action == "deny":
            deny_count += 1
            if dstport:
                deny_ports[dstport] += 1
            if f.get("srcip"):
                deny_src[f["srcip"]] += 1
            if dstport in DVR_PORTS:
                device_deny += 1
                device_ports[dstport] += 1
        elif action in ("accept", "permit"):
            accept_permit += 1

    return {
        "window_days": window_days or [f"{_BASE:%Y-%m-%d}", f"{_BASE:%Y-%m-%d}"],
        "admin_login_failed": sum(admin_fail_src.values()),
        "admin_login_failed_distinct_src": len(admin_fail_src),
        "admin_login_failed_top_src": _top(admin_fail_src, 6),
        "admin_login_disabled_lockouts": lockouts,
        "deny_count": deny_count,
        "deny_top_dstports": _top(deny_ports, 6),
        "deny_top_src": _top(deny_src, 6),
        "accept_permit_count": accept_permit,
        "session_clash": session_clash,
        "fortigate_update_succeeded": update_ok,
        "dhcp_ack_count": dhcp_ack,
        "dhcp_statistics_count": dhcp_stats,
        "security_rating_count": security_rating,
        "device_service_port_deny": device_deny,
        "device_service_port_top": _top(device_ports, 4),
        "performance_sample": perf_sample or {"cpu": "5", "mem": "22", "totalsession": "500"},
    }


# ── injected-incident model ─────────────────────────────────────────────────────
@dataclass
class InjectedIncident:
    """One injected fault: the raw evidence (syslog), the operator symptom (case),
    and the assertion target (expected root cause) the real reasoner is checked against."""

    incident_type: str
    description: str
    syslog_lines: list[str]
    case: RCASeedCase
    expected_root_cause_key: str
    expected_evidence: list[str] = field(default_factory=list)

    def window_stats(self, window_days: list[str] | None = None) -> dict:
        """Derive the stats blob by parsing THIS incident's emitted syslog lines."""
        return aggregate_window_stats(self.syslog_lines, window_days=window_days)


# ── deterministic helpers ───────────────────────────────────────────────────────
def _external_ips(rng: random.Random, n: int) -> list[str]:
    pool = {
        f"{rng.choice((45, 62, 77, 85, 93, 102, 141, 193))}."
        f"{rng.randint(1, 254)}.{rng.randint(0, 254)}.{rng.randint(1, 254)}"
        for _ in range(n * 3)
    }
    return sorted(pool)[:n]


def _baseline_heartbeat(rng: random.Random, start: datetime, *, cpu: int = 5, mem: int = 22) -> list[str]:
    """The always-present background of a real window: perf heartbeat + a little DHCP.

    Harmless to any focused diagnosis — the attention gate never surfaces these
    skills for an operator query that does not name them — but it makes each
    injected window look like a genuine capture rather than a single-signal stub.
    """
    lines: list[str] = []
    for i in range(6):
        lines.append(_line_perf(start + timedelta(minutes=5 * i), cpu, mem, 500 + i * 13, salt=700 + i))
    for i in range(8):
        host = f"192.168.16.{rng.randint(20, 200)}"
        lines.append(_line_dhcp_ack(start + timedelta(minutes=7 * i), host, salt=800 + i))
    return lines


# ── injectors (one per known incident type) ─────────────────────────────────────
def inject_admin_bruteforce_lockout(seed: int = 1101) -> InjectedIncident:
    """External admin brute-force from many source IPs that trips admin-login lockout."""
    rng = random.Random(seed)
    attackers = _external_ips(rng, 60)
    users = ("admin", "recepcion", "rong", "phorn.chayly", "guest", "root", "test", "manager")
    lines: list[str] = []
    t = _BASE
    for i in range(1400):
        srcip = attackers[i % len(attackers)]
        lines.append(_line_admin_fail(t, srcip, users[i % len(users)], salt=i))
        t += timedelta(seconds=11)
    for i in range(6):  # repeated failures from a subset trip the lockout
        lines.append(_line_lockout(_BASE + timedelta(minutes=17 + i), attackers[i], salt=200 + i))
    lines += _baseline_heartbeat(rng, _BASE)
    case = RCASeedCase(
        id="inj_admin_bruteforce_lockout",
        title="Flood of failed admin logins followed by admin-login lockout",
        query="FortiGate shows a flood of failed admin logins and admin login was disabled; what is the root cause?",
        query_terms=["admin", "login", "failed", "lockout", "bruteforce"],
        assets=["fortigate", "DAHUA_FORTIGATE"],
        relevant_skills=["check_admin_auth_failures", "check_admin_lockout"],
    )
    return InjectedIncident(
        incident_type="admin_bruteforce_lockout",
        description="~1400 failed admin logins from 60 external IPs + 6 admin-login-disabled lockouts",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="admin_bruteforce_lockout",
        expected_evidence=["ev-admin-auth-failures", "ev-admin-lockout"],
    )


def inject_internal_policy_deny_flood(seed: int = 2202) -> InjectedIncident:
    """High-volume policy denies from a few chatty internal hosts (NetBIOS et al.)."""
    rng = random.Random(seed)
    hosts = ("192.168.1.43", "192.168.16.56", "192.168.1.20", "192.168.16.29", "192.168.16.73", "192.168.16.66")
    lines: list[str] = []
    t = _BASE
    for i in range(10500):  # must clear the reasoner's deny_count >= 10000 threshold
        host = hosts[i % len(hosts)]
        port = _DENY_PORTS[i % len(_DENY_PORTS)]
        lines.append(_line_policy_deny(t, host, port, salt=i))
        t += timedelta(milliseconds=900)
    for i in range(220):  # accepted flows -> the traffic baseline the case also needs
        lines.append(_line_accept(_BASE + timedelta(seconds=i), f"192.168.16.{20 + (i % 60)}", "208.91.112.61", salt=9000 + i))
    lines += _baseline_heartbeat(rng, _BASE)
    case = RCASeedCase(
        id="inj_internal_policy_deny_flood",
        title="Very high volume of denied flows from internal hosts",
        query="FortiGate is denying a very high volume of flows from internal hosts; what is the root cause?",
        query_terms=["deny", "policy", "port", "traffic", "netbios"],
        assets=["fortigate", "192.168.16.0/20", "192.168.1.0/24"],
        relevant_skills=["check_policy_deny_profile", "check_traffic_baseline"],
    )
    return InjectedIncident(
        incident_type="internal_policy_deny_flood",
        description="~10500 internal denies (NetBIOS/UDP) + 220 accepted flows for baseline",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="internal_policy_deny_expected",
        expected_evidence=["ev-policy-deny-profile", "ev-traffic-baseline"],
    )


def inject_device_port_probe(seed: int = 3303) -> InjectedIncident:
    """External probes denied against Dahua camera/DVR service ports (37777/37809/37810)."""
    rng = random.Random(seed)
    attackers = _external_ips(rng, 25)
    cameras = [f"192.168.30.{n}" for n in (35, 36, 41, 52, 60)]
    weighted = DVR_PORTS[0:1] * 6 + DVR_PORTS[1:2] * 3 + DVR_PORTS[2:3] * 1  # 37777 dominates, as on the real device
    lines: list[str] = []
    t = _BASE
    for i in range(900):
        srcip = attackers[i % len(attackers)]
        dstip = cameras[i % len(cameras)]
        lines.append(_line_device_probe(t, srcip, dstip, weighted[i % len(weighted)], salt=i))
        t += timedelta(seconds=3)
    lines += _baseline_heartbeat(rng, _BASE)
    case = RCASeedCase(
        id="inj_device_port_probe",
        title="Denied probes against camera/DVR service ports",
        query="Many denied flows target camera/DVR service ports such as 37777 and 37809; what is the root cause?",
        query_terms=["camera", "dvr", "device", "probe", "37777"],
        assets=["fortigate", "192.168.30.0/24"],
        relevant_skills=["check_device_port_probe"],
    )
    return InjectedIncident(
        incident_type="device_port_probe",
        description="~900 denied probes to Dahua SDK ports 37777/37809/37810 from 25 external IPs",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="device_service_port_probe_contained",
        expected_evidence=["ev-device-port-probe"],
    )


def inject_dhcp_health(seed: int = 4404) -> InjectedIncident:
    """Healthy DHCP: many ACK + statistics events, leases being issued normally."""
    rng = random.Random(seed)
    lines: list[str] = []
    t = _BASE
    for i in range(600):
        lines.append(_line_dhcp_ack(t, f"192.168.16.{20 + (i % 180)}", salt=i))
        t += timedelta(seconds=30)
    for i in range(30):
        lines.append(_line_dhcp_stats(_BASE + timedelta(minutes=10 * i), salt=5000 + i))
    for i in range(6):
        lines.append(_line_perf(_BASE + timedelta(minutes=5 * i), 5, 22, 500 + i, salt=600 + i))
    case = RCASeedCase(
        id="inj_dhcp_health",
        title="Many DHCP ACK and statistics events",
        query="The FortiGate is emitting many DHCP ACK and statistics events; what is the state of DHCP address allocation?",
        query_terms=["dhcp", "lease", "address", "allocation"],
        assets=["fortigate", "192.168.16.0/20"],
        relevant_skills=["check_dhcp_service"],
    )
    return InjectedIncident(
        incident_type="dhcp_health",
        description="~600 DHCP ACK + 30 DHCP statistics events (healthy lease issuance)",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="dhcp_service_healthy",
        expected_evidence=["ev-dhcp-health"],
    )


def inject_security_posture(seed: int = 5505) -> InjectedIncident:
    """Current security posture: FortiGuard updates succeeding."""
    rng = random.Random(seed)
    lines = [_line_update(_BASE + timedelta(hours=i), salt=i) for i in range(24)]
    lines += _baseline_heartbeat(rng, _BASE)
    case = RCASeedCase(
        id="inj_security_posture",
        title="FortiGuard updates and security-rating summaries",
        query="FortiGuard updates are succeeding and security-rating summaries appear; what is the security posture?",
        query_terms=["fortiguard", "update", "security", "rating", "posture"],
        assets=["fortigate", "DAHUA_FORTIGATE"],
        relevant_skills=["check_security_posture"],
    )
    return InjectedIncident(
        incident_type="security_posture",
        description="24 successful FortiGuard updates over the window",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="security_posture_current",
        expected_evidence=["ev-security-posture"],
    )


def inject_session_clash(seed: int = 6606) -> InjectedIncident:
    """Benign informational session-clash housekeeping events (no fault)."""
    rng = random.Random(seed)
    lines = [_line_session_clash(_BASE + timedelta(seconds=30 * i), salt=i) for i in range(283)]
    for i in range(6):
        lines.append(_line_perf(_BASE + timedelta(minutes=5 * i), 5, 22, 500 + i, salt=600 + i))
    case = RCASeedCase(
        id="inj_session_clash",
        title="Repeated session-clash events in the event log",
        query="The FortiGate event log shows repeated session-clash entries; what is the root cause?",
        query_terms=["event", "session", "clash"],
        assets=["fortigate", "DAHUA_FORTIGATE"],
        relevant_skills=["check_event_log"],
    )
    return InjectedIncident(
        incident_type="session_clash",
        description="283 informational session-clash events (benign housekeeping)",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="benign_session_clash",
        expected_evidence=["ev-event-log-scan"],
    )


def inject_firewall_resource_headroom(seed: int = 7707) -> InjectedIncident:
    """Firewall resource headroom: low CPU/memory, small session table."""
    rng = random.Random(seed)
    lines = [_line_perf(_BASE + timedelta(minutes=3 * i), 4 + (i % 3), 23, 560 + i * 5, salt=i) for i in range(60)]
    for i in range(8):
        lines.append(_line_dhcp_ack(_BASE + timedelta(minutes=7 * i), f"192.168.16.{30 + i}", salt=900 + i))
    case = RCASeedCase(
        id="inj_firewall_resource_headroom",
        title="Firewall CPU/memory/session headroom check",
        query="Is the FortiGate under resource pressure? Check CPU, memory and session headroom.",
        query_terms=["cpu", "memory", "resource", "headroom", "performance"],
        assets=["fortigate", "DAHUA_FORTIGATE"],
        relevant_skills=["check_firewall_resource"],
    )
    return InjectedIncident(
        incident_type="firewall_resource_headroom",
        description="60 perf-stats samples with CPU 4-6% / MEM 23% / ~560 sessions",
        syslog_lines=lines,
        case=case,
        expected_root_cause_key="firewall_resource_healthy",
        expected_evidence=["ev-firewall-resource"],
    )


# Ordered registry of every injector, keyed by incident type.
INJECTORS = {
    "admin_bruteforce_lockout": inject_admin_bruteforce_lockout,
    "internal_policy_deny_flood": inject_internal_policy_deny_flood,
    "device_port_probe": inject_device_port_probe,
    "dhcp_health": inject_dhcp_health,
    "security_posture": inject_security_posture,
    "session_clash": inject_session_clash,
    "firewall_resource_headroom": inject_firewall_resource_headroom,
}


def inject_all() -> list[InjectedIncident]:
    """Craft every known incident, deterministically, newest reasoner-signal first."""
    return [factory() for factory in INJECTORS.values()]


def inject_clean_baseline(seed: int = 9909) -> list[str]:
    """A benign window with NO fault signal — the negative control.

    The same operator symptom case run against these lines must NOT localize a
    fault, proving the diagnosis tracks the injected DATA and is not scripted.
    """
    rng = random.Random(seed)
    lines: list[str] = []
    for i in range(40):
        lines.append(_line_perf(_BASE + timedelta(minutes=5 * i), 5, 22, 500 + i, salt=i))
    for i in range(30):
        lines.append(_line_dhcp_ack(_BASE + timedelta(minutes=3 * i), f"192.168.16.{20 + i}", salt=300 + i))
    for i in range(30):
        lines.append(_line_accept(_BASE + timedelta(seconds=20 * i), f"192.168.16.{40 + i}", "208.91.112.61", salt=600 + i))
    return lines
