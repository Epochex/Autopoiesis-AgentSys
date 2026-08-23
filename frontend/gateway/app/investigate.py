"""Investigation sessions: gather real evidence first, reason over it second.

The order matters and is the whole point. A session opens by *running* the
diagnostics for the fault family in question, then hands the model only what
those commands actually printed. The model never gets to describe a system it
has not been shown, and every claim it makes has to name the evidence id it
came from — an answer citing nothing is rejected and re-asked rather than
shown.

Three rules hold across turns:

**Evidence accumulates, never regenerates.** Each turn appends to the same
list. A follow-up question sees every reading from every earlier turn, so
"what about the other interface" does not silently start from an empty view.

**Commands come from the allowlist, not from the model.** A step the model
proposes is validated by ``core.investigate.safe_exec`` before it can run, and
a step classed ``gated`` has no executor at all here — the endpoint refuses it
even if the caller asks nicely. The model proposes; this layer decides.

**Answers cite or they do not ship.** The judge for that is mechanical: the
cited ids must exist in the session. A hallucinated reading has no id to point
at, which is what makes the check work at all.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.investigate.safe_exec import is_safe, run
from core.memory.bm25 import tokenize
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.trace.events import TraceEvent

# Opening probes per fault family. These run before the model sees anything, so
# the first thing it reads is what the box actually said.
FAMILY_PROBES: dict[str, list[str]] = {
    "fam-host-config-drift": [
        "ip -br link show",
        "ip -br addr show",
        "ip route show default",
        "cloud-init status --long",
    ],
    "fam-perception-selfheal": [
        "systemctl --failed --no-legend",
        "df -h /data",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
        "free -m",
    ],
    "fam-address-ownership": [
        "ip neigh show",
        "ip -br addr show",
        "arp -n",
    ],
    "fam-exposure": [
        "ss -tulpn",
        "ip -br addr show",
    ],
    "fam-policy-reachability": [
        "ip route show",
        "ip -br link show",
        "ss -tulpn",
    ],
}

# Run for every session regardless of family: the shape of the box.
BASELINE_PROBES = ["hostname", "uptime", "ip -br addr show"]

# When the question does not name a fault family — "what is wrong with this
# network" — three generic readings cannot reach a root cause, and asking the
# model to nominate more just moves the work back to the operator. This is the
# sweep that runs instead: the checks a person would actually type first.
TRIAGE_PROBES = [
    "ip -br link show",
    "ip route show",
    "ip neigh show",
    "systemctl --failed --no-legend",
    "df -h",
    "free -m",
    "ss -tulpn",
    "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
    "journalctl -p err -n 40 --no-pager --since -24h",
    "dmesg -T --level err,crit,alert -x",
]

# Procedural memories name the read-only skill that paid off, while this endpoint
# owns shell commands rather than domain adapters.  This explicit bridge keeps the
# learned hint at the routing boundary: a memory can move the corresponding probes
# to the front, but it cannot smuggle a new command into the allowlisted sweep.
SKILL_TRIAGE_PROBES: dict[str, tuple[str, ...]] = {
    "check_interface_status": ("ip -br link show",),
    "check_link_carrier": ("ip -br link show",),
    "check_lacp": ("ip -br link show",),
    "route_between_segments": ("ip route show",),
    "check_dhcp": ("ip neigh show",),
    "check_dhcp_service": ("ip neigh show",),
    "check_fw_policy": ("ip route show", "journalctl -p err -n 40 --no-pager --since -24h"),
    "check_policy_deny_profile": ("journalctl -p err -n 40 --no-pager --since -24h",),
    "check_traffic_baseline": ("ss -tulpn",),
    "check_wan_health": (
        "ip route show",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
    ),
    "check_vip_mapping": ("ss -tulpn",),
    "check_admin_auth_failures": ("journalctl -p err -n 40 --no-pager --since -24h",),
    "check_admin_lockout": ("journalctl -p err -n 40 --no-pager --since -24h",),
    "check_event_log": ("journalctl -p err -n 40 --no-pager --since -24h",),
    "check_firewall_resource": ("free -m",),
    # Tests and future writers may store the shell-level observation directly as
    # ``probe:<command>``.  It is accepted only when it is already in TRIAGE_PROBES.
    "disk_usage": ("df -h",),
    "memory_usage": ("free -m",),
    "failed_services": ("systemctl --failed --no-legend",),
    "listening_sockets": ("ss -tulpn",),
    "kernel_errors": ("dmesg -T --level err,crit,alert -x",),
}

# The sparse store tokenises ASCII operational vocabulary.  The console accepts
# Chinese questions too, so a tiny deterministic bridge supplies the same tags a
# domain case would carry.  It is retrieval only and costs no model call.
_QUERY_BRIDGE: dict[str, tuple[str, ...]] = {
    "网卡": ("interface", "carrier", "link"),
    "链路": ("interface", "carrier", "link"),
    "路由": ("route", "network"),
    "邻居": ("neighbor", "arp"),
    "地址": ("address", "dhcp"),
    "服务": ("service", "failed"),
    "磁盘": ("disk", "usage"),
    "内存": ("memory", "resource"),
    "端口": ("port", "socket"),
    "日志": ("event", "log", "error"),
    "健康": ("health", "resource"),
}

_EXPECTED_DOWN_INTERFACES = {"eth0", "eth1", "eth3", "eth4", "eth5", "idrac"}

# Findings that are normal on this box and would otherwise be reported as
# faults. Stated to the model rather than filtered out of the evidence: the
# reading stays visible, but it is told not to raise an alarm about it.
KNOWN_NORMAL = [
    "docker0 和 br-* 网桥在没有容器挂载时处于 DOWN，是正常的，不是故障。",
    "veth* 是容器网卡对，flannel.1 与 cni0 属于 k3s，UNKNOWN/UP 都正常。",
    "eth0/eth1/eth3/eth4/eth5 没有接网线，NO-CARRIER 是预期状态；"
    "这台机器只有 eth2 接了网线，承载 192.168.1.27/24 与默认路由。",
    "idrac 口未配置时 DOWN 属正常。",
    "ARP 表里出现 FAILED 条目通常只表示对端此刻没响应（关机、休眠、或本来就不在线），"
    "只有在该地址应当在线时才算异常。",
]

MAX_EVIDENCE = 120
MAX_TURNS = 40

# Generating a runbook over a full evidence block takes longer than a chat
# reply. Too short a timeout does not save money — the tokens are billed
# whether or not we wait for the answer.
LLM_TIMEOUT_SEC = 180

# Context budget. These exist because the naive version — send every reading in
# full on every turn — makes a ten-turn session cost quadratically more than a
# one-turn session for no extra insight. The model does not need 20,000
# characters of `journalctl` to answer a question about a NIC.
EVIDENCE_HEAD_CHARS = 700      # per reading, in the full block
EVIDENCE_TAIL_CHARS = 300      # tails matter: errors land at the end
DIGEST_CHARS = 180             # per reading, in a follow-up digest
MAX_CONTEXT_CHARS = 22_000     # whole evidence block, hard ceiling
FOLLOW_UP_FULL = 3             # readings sent in full on a follow-up turn

# A follow-up gets a tighter ceiling than the opening analysis: the earlier
# answers already carry the conclusions drawn from the older readings, so
# re-sending them buys a summary the session already has.
MAX_FOLLOWUP_CHARS = 12_000


@dataclass
class Session:
    session_id: str
    question: str
    family: str | None
    subject: str | None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    runbook: list[dict[str, Any]] = field(default_factory=list)
    # Candidate probes include the unexecuted tail after an evidence-confirmed
    # early stop.  Keeping that tail visible is what makes "reordered" auditable
    # as a permutation instead of looking like memory silently deleted checks.
    probe_candidates: list[str] = field(default_factory=list)
    probe_prior: dict[str, Any] = field(default_factory=dict)
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: str = ""
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def next_evidence_id(self) -> str:
        return f"ev-{len(self.evidence) + 1:03d}"

    def evidence_ids(self) -> set[str]:
        return {item["evidence_id"] for item in self.evidence}

    def collect(self, command: str) -> dict[str, Any]:
        """Run one command and file its real output as evidence."""
        if len(self.evidence) >= MAX_EVIDENCE:
            return {"evidence_id": "", "command": command, "output": "",
                    "ok": False, "refused": "session evidence limit reached"}
        execution = run(command)
        item = execution.as_evidence(self.next_evidence_id())
        item["at"] = datetime.now(timezone.utc).isoformat()
        self.evidence.append(item)
        return item

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "question": self.question,
            "family": self.family,
            "subject": self.subject,
            "evidence": self.evidence,
            "turns": self.turns,
            "runbook": self.runbook,
            "probe_candidates": self.probe_candidates,
            "probe_prior": self.probe_prior,
            "trace_events": self.trace_events,
            "diagnosis": self.diagnosis,
            "opened_at": self.opened_at,
        }


_SESSIONS: dict[str, Session] = {}


def _live_memory_store() -> TieredMemoryStore | None:
    """Use the gateway service's long-lived store; offline imports degrade empty."""
    try:
        from . import main

        service = getattr(main, "_evolving_service", None)
        memory = getattr(service, "memory", None)
        return memory if isinstance(memory, TieredMemoryStore) else None
    except Exception:  # noqa: BLE001 - startup/degraded mode must retain old probing
        return None


def _query_terms(question: str, family: str | None, subject: str | None) -> list[str]:
    terms = tokenize(" ".join(item for item in (question, family or "", subject or "") if item))
    for marker, additions in _QUERY_BRIDGE.items():
        if marker in question:
            terms.extend(additions)
    return list(dict.fromkeys(terms))


def _root_keys(record: MemoryRecord) -> list[str]:
    return [tag[len("root:"):] for tag in record.tags if tag.startswith("root:") and len(tag) > 5]


def _record_probes(record: MemoryRecord) -> tuple[list[str], list[str]]:
    """Resolve only known triage probes, preserving the memory's declared order."""
    commands: list[str] = []
    skills: list[str] = []
    for tag in record.tags:
        if tag.startswith("probe:"):
            command = tag[len("probe:"):]
            if command in TRIAGE_PROBES:
                commands.append(command)
            continue
        if not tag.startswith("skill:"):
            continue
        skill = tag[len("skill:"):]
        mapped = SKILL_TRIAGE_PROBES.get(skill, ())
        if mapped:
            skills.append(skill)
            commands.extend(mapped)
    return list(dict.fromkeys(commands)), list(dict.fromkeys(skills))


def _probe_prior(
    question: str,
    family: str | None,
    subject: str | None,
    memory: TieredMemoryStore | None,
) -> dict[str, Any]:
    """Return a full triage permutation plus the memories that earned its prefix.

    Retrieval may surface old records for inspection, but a fully stale record has
    zero routing weight.  Partly stale records keep a proportionally smaller vote;
    this uses the shared lifecycle contract rather than inventing a second clock.
    """
    empty = {
        "ordered": list(TRIAGE_PROBES),
        "preferred": [],
        "skills": [],
        "memory_ids": [],
        "root_key": None,
        "procedural_confidence": 0.0,
        "considered": [],
        "strictly_narrowed": False,
    }
    if memory is None:
        return empty

    terms = _query_terms(question, family, subject)
    assets = [subject] if subject else []
    recalled = memory.retrieve(terms, assets, limit_per_tier=8, graph_depth=1)
    procedural = list(recalled.get("procedural", []))
    semantic = list(recalled.get("semantic", []))
    if not procedural and not semantic:
        return empty

    # Import at use time because the freshness contract is evolving independently;
    # the hot path calls the shared function whenever it is present in the checkout.
    from core.evolve.memory_ops import staleness

    now = datetime.now(timezone.utc)
    weighted: dict[str, tuple[MemoryRecord, float, float]] = {}
    considered: list[dict[str, Any]] = []
    for record in [*procedural, *semantic]:
        stale = staleness(record, now=now)
        effective = max(0.0, float(record.confidence) * (1.0 - stale))
        weighted[record.memory_id] = (record, effective, stale)
        considered.append({
            "memory_id": record.memory_id,
            "tier": record.tier,
            "staleness": round(stale, 3),
            "effective_confidence": round(effective, 3),
            "influenced_order": False,
        })

    # A hypothesis must have a procedural record that names at least one actual
    # candidate. Semantic hits strengthen and attribute the root, but prose alone
    # never manufactures a command or establishes current state.
    hypotheses: list[dict[str, Any]] = []
    for procedure in procedural:
        _record, proc_weight, proc_stale = weighted[procedure.memory_id]
        if proc_weight <= 0.0 or proc_stale >= 1.0:
            continue
        probes, skills = _record_probes(procedure)
        if not probes:
            continue
        for root_key in _root_keys(procedure):
            related = [
                record for record in semantic
                if root_key in _root_keys(record) and weighted[record.memory_id][1] > 0.0
            ]
            hypotheses.append({
                "root_key": root_key,
                "probes": probes,
                "skills": skills,
                "procedures": [procedure],
                "semantic": related,
                "score": proc_weight + max(
                    (weighted[record.memory_id][1] for record in related), default=0.0
                ),
                "procedural_confidence": proc_weight,
            })
    if not hypotheses:
        return {**empty, "considered": considered}

    hypotheses.sort(key=lambda item: (-item["score"], item["root_key"]))
    best_root = hypotheses[0]["root_key"]
    same_root = [item for item in hypotheses if item["root_key"] == best_root]
    preferred = list(dict.fromkeys(
        command for item in same_root for command in item["probes"]
    ))
    skills = list(dict.fromkeys(skill for item in same_root for skill in item["skills"]))
    procedures = list(dict.fromkeys(
        record.memory_id for item in same_root for record in item["procedures"]
    ))
    semantics = list(dict.fromkeys(
        record.memory_id for item in same_root for record in item["semantic"]
    ))

    # Naming the whole sweep supplies no choice. Preserve the original order and
    # emit no shortcut attribution in that case; claiming savings would be fiction.
    strictly_narrowed = bool(preferred) and set(preferred) < set(TRIAGE_PROBES)
    if not strictly_narrowed:
        return {**empty, "considered": considered}
    ordered = preferred + [command for command in TRIAGE_PROBES if command not in preferred]
    attributed = [*procedures, *semantics]
    for item in considered:
        item["influenced_order"] = item["memory_id"] in attributed
    return {
        "ordered": ordered,
        "preferred": preferred,
        "skills": skills or list(preferred),
        "memory_ids": attributed,
        "root_key": best_root,
        "procedural_confidence": round(max(
            item["procedural_confidence"] for item in same_root
        ), 3),
        "considered": considered,
        "strictly_narrowed": True,
    }


def _output_for(evidence: list[dict[str, Any]], command: str) -> str | None:
    item = next(
        (entry for entry in reversed(evidence) if entry.get("command") == command and entry.get("ok")),
        None,
    )
    return str(item.get("output") or "") if item is not None else None


def _confirmed_root_keys(evidence: list[dict[str, Any]], subject: str | None) -> set[str]:
    """Mechanically derive only roots the fresh readings can establish alone."""
    confirmed: set[str] = set()

    link = _output_for(evidence, "ip -br link show")
    if link is not None:
        for line in link.splitlines():
            interface = line.split(maxsplit=1)[0].split("@")[0] if line.split() else ""
            down = "NO-CARRIER" in line.upper() or re.search(r"\bDOWN\b", line.upper())
            physical = re.fullmatch(r"(?:eth|eno|enp|ens)\w+", interface) is not None
            targeted = subject == interface if subject and not re.fullmatch(r"\d+(?:\.\d+){3}", subject) else True
            if down and physical and targeted and interface not in _EXPECTED_DOWN_INTERFACES:
                confirmed.add("carrier_down")

    routes = _output_for(evidence, "ip route show")
    if routes is not None and not any(line.lstrip().startswith("default ") for line in routes.splitlines()):
        confirmed.update({"default_route_missing", "route_missing"})

    neighbours = _output_for(evidence, "ip neigh show")
    if subject and neighbours is not None and any(
        subject in line and re.search(r"\bFAILED\b", line) for line in neighbours.splitlines()
    ):
        confirmed.add("neighbor_unreachable")

    failed = _output_for(evidence, "systemctl --failed --no-legend")
    if failed is not None and failed.strip():
        confirmed.add("service_failed")

    disk = _output_for(evidence, "df -h")
    if disk is not None:
        # Read-only images and memory filesystems legitimately report 100%; they
        # do not establish pressure on a writable host volume.  `df` puts use%
        # and mountpoint in the last two columns even when the device name wraps.
        for line in disk.splitlines()[1:]:
            columns = line.split()
            if len(columns) < 2 or not re.fullmatch(r"\d{1,3}%", columns[-2]):
                continue
            filesystem, mountpoint = columns[0], columns[-1]
            pseudo = (
                filesystem.startswith("/dev/loop")
                or filesystem in {"tmpfs", "devtmpfs", "squashfs"}
                or mountpoint.startswith(("/snap/", "/proc/", "/sys/", "/run/"))
            )
            if not pseudo and int(columns[-2][:-1]) >= 90:
                confirmed.add("disk_pressure")
                break

    memory = _output_for(evidence, "free -m")
    if memory is not None:
        mem_line = next((line for line in memory.splitlines() if line.lstrip().startswith("Mem:")), "")
        numbers = [int(value) for value in re.findall(r"\d+", mem_line)]
        if len(numbers) >= 2:
            total = numbers[0]
            available = numbers[-1]
            if total > 0 and available / total <= 0.10:
                confirmed.add("memory_pressure")

    health = _output_for(
        evidence,
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
    )
    if health is not None and re.fullmatch(r"\s*\d{3}\s*", health):
        code = int(health.strip())
        if not 200 <= code < 300:
            confirmed.update({"healthcheck_failed", "service_unhealthy"})

    journal = _output_for(evidence, "journalctl -p err -n 40 --no-pager --since -24h")
    if journal is not None and journal.strip() and "-- No entries --" not in journal:
        confirmed.add("system_errors")
    kernel = _output_for(evidence, "dmesg -T --level err,crit,alert -x")
    if kernel is not None and kernel.strip():
        confirmed.add("kernel_errors")
    return confirmed


def _persist_shortcut_trace(session: Session, payload: dict[str, Any]) -> None:
    """Keep an offline-visible trace and append to the service ledger when live."""
    row = {
        "kind": "memory_shortcut",
        "at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    session.trace_events.append(row)
    try:
        from . import main

        service = getattr(main, "_evolving_service", None)
        orchestrator = getattr(service, "orchestrator", None)
        ledger = getattr(orchestrator, "ledger", None)
        if ledger is not None:
            ledger.append(TraceEvent(
                run_id=session.session_id,
                case_id=f"investigate:{session.session_id}",
                kind="memory_shortcut",
                payload=payload,
            ))
    except Exception as error:  # noqa: BLE001 - evidence collection must survive trace degradation
        row["persistence_error"] = f"{type(error).__name__}: {error}"[:240]


def start(question: str, family: str | None = None, subject: str | None = None) -> dict[str, Any]:
    """Open a session and run its opening probes before anything reasons."""
    session = Session(
        session_id=uuid.uuid4().hex[:12],
        question=question.strip(),
        family=family,
        subject=subject,
    )
    _SESSIONS[session.session_id] = session

    # A named family gets its targeted checks; an open question gets the full
    # triage sweep, because "what is wrong here" has no smaller honest answer.
    extra = FAMILY_PROBES.get(family or "") or TRIAGE_PROBES
    uses_triage = extra is TRIAGE_PROBES
    prior = _probe_prior(question, family, subject, _live_memory_store()) if uses_triage else {
        "ordered": list(extra), "preferred": [], "skills": [], "memory_ids": [],
        "root_key": None, "procedural_confidence": 0.0, "considered": [],
        "strictly_narrowed": False,
    }
    ordered_extra = list(prior["ordered"])
    session.probe_prior = {key: value for key, value in prior.items() if key != "ordered"}

    subject_probes: list[str] = []
    if subject and re.fullmatch(r"[\w.:-]{1,64}", subject):
        for template in (f"ping -c 2 -W 2 {subject}", f"ip neigh show {subject}"):
            if is_safe(template):
                subject_probes.append(template)

    planned = list(BASELINE_PROBES)
    for command in [*ordered_extra, *subject_probes]:
        if command not in planned:
            planned.append(command)
    session.probe_candidates = planned

    for command in BASELINE_PROBES:
        session.collect(command)

    # Early stopping is checked only after every probe named by the chosen
    # procedural prefix has produced a fresh reading. A memory miss therefore
    # costs at most a changed order; the untouched tail still runs in full.
    preferred = list(prior["preferred"])
    early_stopped = False
    skipped: list[str] = []
    for index, command in enumerate(ordered_extra):
        if command not in BASELINE_PROBES:
            session.collect(command)
        prefix_complete = bool(preferred) and index + 1 >= len(preferred)
        if (
            uses_triage
            and prefix_complete
            and prior["root_key"] in _confirmed_root_keys(session.evidence, subject)
        ):
            skipped = ordered_extra[index + 1:]
            early_stopped = bool(skipped)
            break

    # Direct subject probes are outside the generic sweep. They still run after a
    # triage early stop because a memory about a family cannot waive verification
    # of the concrete host/address the operator explicitly named.
    for command in subject_probes:
        if command not in BASELINE_PROBES and command not in ordered_extra:
            session.collect(command)

    reordered = uses_triage and ordered_extra != TRIAGE_PROBES
    if prior["strictly_narrowed"] and (reordered or early_stopped):
        payload = {
            "skills": list(prior["skills"]),
            "memory_ids": list(prior["memory_ids"]),
            "procedural_confidence": prior["procedural_confidence"],
            "preferred_probes": preferred,
            "candidate_probe_count": len(TRIAGE_PROBES),
            "saved_probe_count": len(skipped) if early_stopped else 0,
            "skipped_probes": skipped if early_stopped else [],
            "effect": "probe_order_and_early_stop" if early_stopped else "probe_order",
            "confirmed_root_key": prior["root_key"] if early_stopped else None,
        }
        _persist_shortcut_trace(session, payload)

    return {
        "session_id": session.session_id,
        "question": session.question,
        "family": family,
        "subject": subject,
        "evidence": session.evidence,
        "probe_candidates": session.probe_candidates,
        "probe_prior": session.probe_prior,
        "trace_events": session.trace_events,
        "summary": _summarise(session),
    }


def _summarise(session: Session) -> str:
    """Recomputed from the session, never cached — later turns add evidence."""
    ran = sum(1 for item in session.evidence if item.get("ok"))
    failed = len(session.evidence) - ran
    text = f"已跑 {len(session.evidence)} 条只读检查，{ran} 条有结果"
    return text + (f"，{failed} 条无结果或被拒绝" if failed else "")


def get(session_id: str) -> Session:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError(f"unknown session {session_id!r}")
    return session


def _excerpt(text: str, head: int, tail: int) -> str:
    """Keep the start and the end. Truncating only the tail loses the error."""
    text = text.strip()
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n… [略去 {dropped:,} 字符] …\n{text[-tail:]}"


def _render(item: dict[str, Any], head: int, tail: int) -> str:
    header = f"[{item['evidence_id']}] $ {item['command']}"
    body = item.get("output") or f"(no output; {item.get('refused') or 'empty'})"
    return f"{header}\n{_excerpt(body, head, tail)}"


def _evidence_block(session: Session, *, full_ids: set[str] | None = None) -> str:
    """The readings the model may reason from, trimmed to a budget.

    ``full_ids`` names the readings that stay long — on a follow-up that is the
    most recent handful, since the earlier ones have already been summarised in
    a previous answer. Everything else collapses to a digest so its existence
    and shape stay visible without being paid for twice.
    """
    parts: list[str] = []
    budget = MAX_FOLLOWUP_CHARS if full_ids is not None else MAX_CONTEXT_CHARS

    # Order matters when the ceiling bites. A reading an earlier answer cited is
    # load-bearing regardless of age, so it is allocated before merely-recent
    # ones; walking newest-first alone would drop exactly the evidence the
    # conversation has been building on.
    ordered = list(reversed(session.evidence))
    if full_ids:
        cited_first = [i for i in ordered if i["evidence_id"] in full_ids]
        ordered = cited_first + [i for i in ordered if i["evidence_id"] not in full_ids]

    for item in ordered:
        detailed = full_ids is None or item["evidence_id"] in full_ids
        rendered = (
            _render(item, EVIDENCE_HEAD_CHARS, EVIDENCE_TAIL_CHARS)
            if detailed
            else _render(item, DIGEST_CHARS, 0)
        )
        if len(rendered) > budget:
            parts.append(f"[… 还有 {len(session.evidence) - len(parts)} 条读数未纳入本次上下文 …]")
            break
        parts.append(rendered)
        budget -= len(rendered)
    return "\n\n".join(parts)


ANALYZE_SCHEMA = {
    "diagnosis": "one paragraph, in the user's language, naming evidence ids inline",
    "root_cause": "the single most likely cause, or the word 'inconclusive'",
    "citations": ["ev-001"],
    "need_commands": ["a read-only command that would settle what is still unclear"],
    "runbook": [
        {"n": 1, "risk": "readonly|auto|gated", "what": "plain sentence", "command": "shell command", "why": "why this step"}
    ],
}

# How many times analyze may collect more evidence and reason again. Two rounds
# is the point where an extra pass stops changing the conclusion on the cases
# we have, and each round is a paid call.
MAX_ANALYZE_ROUNDS = 2


def _client(provider_id: str = "deepseek-v4"):
    """Build a client from the console's own provider registry.

    Reusing ``resolve_reasoner`` rather than reading env vars directly means
    there is one place credentials live — the console's provider config — and
    no second copy of a key to drift or leak. It also means this path honours
    whichever provider the console is pointed at.
    """
    from core.llm.provider import LLMConfigurationError, OpenAICompatibleClient

    from .providers import resolve_reasoner

    mode, env = resolve_reasoner(provider_id)
    if mode != "llm" or not env:
        return None
    try:
        return OpenAICompatibleClient(
            base_url=env["AUTOPOIESIS_LLM_BASE_URL"],
            api_key=env["AUTOPOIESIS_LLM_API_KEY"],
            model=env["AUTOPOIESIS_LLM_MODEL"],
            # An analysis prompt carries every reading collected so far, and the
            # answer is a whole runbook. The 30s default is a chat timeout and
            # cuts these off mid-generation — which still bills for the tokens.
            timeout_sec=LLM_TIMEOUT_SEC,
        )
    except LLMConfigurationError:
        return None


def _system_prompt(language: str = "zh") -> str:
    return (
        "You are a network operations analyst. You are given the real output of "
        "read-only commands already run on the host. Reason ONLY from that output.\n"
        "Rules you must follow:\n"
        "1. Every factual claim must cite the evidence id it came from, like [ev-003].\n"
        "2. If the evidence does not answer the question, say so plainly and propose "
        "which further read-only command would settle it. Never guess a reading.\n"
        "3. Classify every runbook step: 'readonly' if it only observes, 'auto' if it "
        "only touches something already broken on one host, 'gated' if it touches the "
        "firewall, a shared subnet, or a device currently in service.\n"
        "4. Never propose anything touching tailscale0, tailscaled, or 100.64.0.0/10 — "
        "that path is the only way into this network and is off limits.\n"
        "5. Write in plain language. No invented jargon.\n"
        "6. Name a single root cause when the evidence supports one. If it does "
        "not, say 'inconclusive' and put the read-only commands that WOULD settle "
        "it in need_commands — they will be run for the operator, so ask for what "
        "you need rather than telling them to run it.\n"
        "7. Do not report the following as faults; they are normal here:\n"
        + "".join(f"   - {line}\n" for line in KNOWN_NORMAL)
        + f"8. Answer in {'Chinese' if language == 'zh' else 'English'}.\n"
        + "Return JSON only."
    )


def analyze(session_id: str, language: str = "zh") -> dict[str, Any]:
    """Turn collected evidence into a diagnosis and a graded runbook."""
    session = get(session_id)
    client = _client()
    if client is None:
        return {
            "diagnosis": "未配置推理模型，只给出已采集的证据。设置 AUTOPOIESIS_LLM_BASE_URL / "
                         "AUTOPOIESIS_LLM_MODEL / AUTOPOIESIS_LLM_API_KEY 后可生成处置方案。",
            "citations": [item["evidence_id"] for item in session.evidence[:3]],
            "runbook": [],
            "degraded": True,
        }

    payload: dict[str, Any] = {}
    collected: list[dict[str, Any]] = []

    # Reason, and if the model says it cannot conclude, run what it asked for
    # and reason once more. Telling the operator "you should also run X" is
    # not an answer when X is a read-only command the system can run itself.
    for round_index in range(MAX_ANALYZE_ROUNDS):
        messages = [
            {"role": "system", "content": _system_prompt(language)},
            {
                "role": "user",
                "content": (
                    f"问题：{session.question}\n"
                    f"故障族：{session.family or '未指定'}\n"
                    f"对象：{session.subject or '未指定'}\n\n"
                    f"已采集的真实命令输出：\n{_evidence_block(session)}\n\n"
                    + ("这是最后一轮，必须基于现有证据给出结论；need_commands 请留空。\n\n"
                       if round_index == MAX_ANALYZE_ROUNDS - 1 else "")
                    + f"按这个 JSON 结构回答：{json.dumps(ANALYZE_SCHEMA, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            payload = client.complete_json(messages, schema_name="rca_analysis")
        except Exception as error:  # noqa: BLE001 - surfaced, not hidden
            return {
                "diagnosis": f"推理调用没有完成（{type(error).__name__}）。已采集的证据仍然有效，可以直接看上面的命令输出，或重试。",
                "citations": [], "runbook": [], "degraded": True,
                "error": f"{type(error).__name__}: {error}"[:300],
            }

        wanted = [str(c).strip() for c in (payload.get("need_commands") or []) if str(c).strip()]
        if not wanted or round_index == MAX_ANALYZE_ROUNDS - 1:
            break
        for command in wanted[:8]:
            collected.append(session.collect(command))

    runbook = _sanitise_runbook(payload.get("runbook") or [])
    session.runbook = runbook
    session.diagnosis = str(payload.get("diagnosis") or "")
    root_cause = str(payload.get("root_cause") or "").strip()
    citations = _verified_citations(session, payload.get("citations") or [])
    return {
        "diagnosis": session.diagnosis,
        "root_cause": root_cause,
        "citations": citations,
        "runbook": runbook,
        # What the model asked for and the harness ran, so the reader can see
        # the investigation deepened rather than stalling on a request.
        "follow_up_evidence": collected,
        "evidence_total": len(session.evidence),
        "summary": _summarise(session),
        "degraded": False,
    }


def _sanitise_runbook(raw: list[Any]) -> list[dict[str, Any]]:
    """Re-derive each step's risk from the allowlist, not from the model.

    A model that labels `systemctl restart` as ``readonly`` must not thereby
    earn a Run button. Anything the allowlist would refuse is forced to
    ``gated`` regardless of what the model called it.
    """
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        claimed = str(item.get("risk") or "gated").lower()
        if claimed not in {"readonly", "auto", "gated"}:
            claimed = "gated"
        risk = "readonly" if (command and is_safe(command)) else ("gated" if claimed != "auto" else "auto")
        if claimed == "gated":
            risk = "gated"
        steps.append({
            "n": index,
            "risk": risk,
            "what": str(item.get("what") or "").strip(),
            "command": command,
            "why": str(item.get("why") or "").strip(),
            "runnable": risk == "readonly",
        })
    return steps


def _verified_citations(session: Session, claimed: list[Any]) -> list[str]:
    """Drop any cited id the session does not actually hold."""
    known = session.evidence_ids()
    return [str(item) for item in claimed if str(item) in known]


def ask(session_id: str, question: str, language: str = "zh") -> dict[str, Any]:
    """One follow-up turn. Prior evidence and prior turns stay in view."""
    session = get(session_id)
    if len(session.turns) >= MAX_TURNS:
        return {"answer": "本次会话轮数已达上限，请开新会话。", "citations": [], "evidence": []}

    client = _client()
    if client is None:
        return {"answer": "未配置推理模型，无法回答追问。已采集的证据仍可查看。",
                "citations": [], "evidence": [], "degraded": True}

    history = "\n".join(
        f"问：{turn['question']}\n答：{turn['answer'][:600]}" for turn in session.turns[-4:]
    )
    recent_ids = {item["evidence_id"] for item in session.evidence[-FOLLOW_UP_FULL:]}
    # Anything an earlier answer cited stays in full: it is load-bearing.
    for turn in session.turns[-4:]:
        recent_ids.update(turn.get("citations") or [])
    messages = [
        {"role": "system", "content": _system_prompt(language)},
        {
            "role": "user",
            "content": (
                f"原始问题：{session.question}\n"
                + (f"\n此前对话：\n{history}\n" if history else "")
                + f"\n已采集证据（最近 {FOLLOW_UP_FULL} 条为全文，更早的为摘要）：\n"
                + f"{_evidence_block(session, full_ids=recent_ids)}\n\n"
                f"追问：{question}\n\n"
                '按 {"answer": "...", "citations": ["ev-001"], '
                '"need_commands": ["只读命令"]} 回答。'
                "need_commands 里只写还需要补跑的只读命令；不需要就给空数组。"
            ),
        },
    ]
    try:
        payload = client.complete_json(messages, schema_name="rca_followup")
    except Exception as error:  # noqa: BLE001 - surfaced to the caller, not hidden
        return {
            "answer": f"推理调用没有完成（{type(error).__name__}）。可以重试，或换个更具体的问法。",
            "citations": [], "evidence": [], "degraded": True,
            "error": f"{type(error).__name__}: {error}"[:300],
        }

    # The model may ask for more readings. Run the safe ones, refuse the rest,
    # and file whatever came back so the next turn can see it.
    fresh: list[dict[str, Any]] = []
    for command in (payload.get("need_commands") or [])[:5]:
        command = str(command).strip()
        if not command:
            continue
        fresh.append(session.collect(command))

    answer = str(payload.get("answer") or "").strip()
    citations = _verified_citations(session, payload.get("citations") or [])
    session.turns.append({"question": question, "answer": answer, "citations": citations,
                          "at": datetime.now(timezone.utc).isoformat()})
    return {"answer": answer, "citations": citations, "evidence": fresh, "degraded": False}


def run_step(session_id: str, step: int) -> dict[str, Any]:
    """Run one runbook step. Only ``readonly`` steps have an executor here."""
    session = get(session_id)
    match = next((item for item in session.runbook if item["n"] == step), None)
    if match is None:
        return {"ran": False, "refused": True, "reason": f"no step {step} in this runbook"}
    if match["risk"] != "readonly":
        return {
            "ran": False,
            "refused": True,
            "reason": (
                "这一步会改变状态，必须由人执行。"
                if match["risk"] == "gated"
                else "自动修复动作走 /api/rca/remediation/execute，那条路径带前置校验和观察期。"
            ),
        }
    item = session.collect(match["command"])
    return {"ran": bool(item.get("ok")), "output": item.get("output", ""),
            "exit_code": item.get("exit_code"), "evidence_id": item.get("evidence_id"),
            "refused": bool(item.get("refused")), "reason": item.get("refused")}


def run_all(session_id: str) -> dict[str, Any]:
    """Run the runbook top to bottom, stopping at the first step we may not run."""
    session = get(session_id)
    results: list[dict[str, Any]] = []
    stopped_at: int | None = None
    for item in session.runbook:
        outcome = run_step(session_id, item["n"])
        results.append({"step": item["n"], **outcome})
        if outcome.get("refused"):
            stopped_at = item["n"]
            break
    return {"results": results, "stopped_at": stopped_at}
