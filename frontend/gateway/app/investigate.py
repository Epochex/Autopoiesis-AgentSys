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
import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from core.investigate.evidence_retrieval import (
    EvidenceCandidate,
    EvidenceRetriever,
    RetrievalScope,
)
from core.investigate.hypothesis_loop import (
    EvidenceInput,
    HypothesisLoop,
    ProbeCandidate,
    RootCauseHypothesis,
)
from core.investigate.observation_predicate import (
    ObservationPredicate,
    evaluate_observation,
)
from core.investigate.network_hypotheses import (
    ACTIVE_ROOTS as _ACTIVE_ROOTS,
    EXPECTED_DOWN_INTERFACES as _EXPECTED_DOWN_INTERFACES,
    FAMILY_ACTIVE_ROOTS as _FAMILY_ACTIVE_ROOTS,
    command_matches_probe as _command_matches_probe,
    create_network_hypothesis_loop,
    probe_observation as _probe_polarity,
)
from core.investigate.safe_exec import is_safe, run
from core.memory.bm25 import tokenize
from core.memory.ops_knowledge import retrieve_ops_knowledge
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.trace.events import TraceEvent

# Opening probes per fault family. These run before the model sees anything, so
# the first thing it reads is what the box actually said.
FAMILY_PROBES: dict[str, list[str]] = {
    "fam-management-auth": ["adapter:admin_auth_window"],
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
        "adapter:environment_finding",
        "ip neigh show",
        "ip -br addr show",
    ],
    "fam-exposure": [
        "ss -tulpn",
        "ip -br addr show",
    ],
    "fam-policy-reachability": [
        "adapter:case_flow_window",
        "adapter:fortigate_context",
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

# An open investigation starts with enough checks to separate network path and
# host-service causes.  The remaining candidates stay available for the next
# evidence round, so the opening request does not blindly execute the whole
# catalogue.
OPENING_ACTIVE_PROBE_BUDGET = 4

# A device portrait is a routing hint, never evidence.  Each anomaly names only
# existing read-only checks whose fresh output can investigate that observation.
# The helper below always returns a permutation of its input, so an unavailable or
# mistaken portrait can cost ordering only; it cannot waive a check or confirm a
# diagnosis.
PROFILE_TRIAGE_PROBES: dict[str, tuple[str, ...]] = {
    "first_deny": (
        "journalctl -p err -n 40 --no-pager --since -24h",
        "ip route show",
    ),
    "new_peer": (
        "ip neigh show",
        "ss -tulpn",
    ),
    "new_interface": ("ip -br link show",),
    "session_spike": ("ss -tulpn",),
    "peer_outlier": ("ss -tulpn", "ip route show"),
    "volume_outlier": ("ss -tulpn", "ip -br link show"),
}


def order_triage_by_profile(
    ordered: list[str], anomaly_types: list[str] | tuple[str, ...]
) -> list[str]:
    """Move portrait-relevant checks forward while preserving the full set."""
    preferred = list(dict.fromkeys(
        command
        for anomaly_type in anomaly_types
        for command in PROFILE_TRIAGE_PROBES.get(anomaly_type, ())
        if command in ordered
    ))
    return preferred + [command for command in ordered if command not in preferred]


def _device_profile_anomaly_types(subject: str | None) -> list[str]:
    """Rebuild one device's latest portrait decision from read-only fact rows.

    This deliberately fails closed to an empty hint.  ClickHouse supplies history,
    while ``ProfileStore`` owns every anomaly rule and raw-count comparison; this
    endpoint does not duplicate or loosen those rules.
    """
    if subject is None or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", subject) is None:
        return []
    try:
        from domains.network_rca.device_profile import ProfileStore
        from domains.network_rca.fortigate_stream import FortiEvent
        from .history import _CH_DB, _q

        safe_subject = ".".join(str(int(part)) for part in subject.split("."))
        if any(not 0 <= int(part) <= 255 for part in safe_subject.split(".")):
            return []
        rows = _q(
            "SELECT event_ts, srcip, dstip, dstport, proto, action, type, subtype, "
            "srcintf, dstintf, sentbyte, rcvdbyte "
            f"FROM {_CH_DB}.facts WHERE srcip='{safe_subject}' "
            "AND event_ts >= (SELECT max(event_ts) - INTERVAL 7 DAY "
            f"FROM {_CH_DB}.facts WHERE srcip='{safe_subject}') "
            "ORDER BY event_ts DESC LIMIT 5001"
        )
        if not rows:
            return []

        def event(row: dict[str, Any]) -> FortiEvent:
            raw_at = str(row["event_ts"])
            at = datetime.fromisoformat(raw_at.replace(" ", "T"))
            at = (
                at.replace(tzinfo=timezone.utc)
                if at.tzinfo is None
                else at.astimezone(timezone.utc)
            )
            return FortiEvent(
                at=at,
                logid="clickhouse-fact",
                type=str(row.get("type") or "traffic"),
                subtype=str(row.get("subtype") or "forward"),
                level="notice",
                action=str(row.get("action") or "") or None,
                src_ip=str(row.get("srcip") or safe_subject),
                dst_ip=str(row.get("dstip") or "") or None,
                src_port=None,
                dst_port=int(row.get("dstport") or 0) or None,
                proto=int(row.get("proto") or 0) or None,
                src_intf=str(row.get("srcintf") or "") or None,
                dst_intf=str(row.get("dstintf") or "") or None,
                user=None,
                logdesc=None,
                msg=None,
                status=None,
                sent_bytes=int(row.get("sentbyte") or 0),
                rcvd_bytes=int(row.get("rcvdbyte") or 0),
                raw={key: str(value) for key, value in row.items()},
            )

        events = sorted((event(row) for row in rows), key=lambda item: item.at)
        candidate = events.pop()
        profile = ProfileStore()
        for item in events:
            profile.observe(item)
        # Cohort rules need the whole /24, while replaying millions of flows in
        # a request is wasteful.  ClickHouse supplies exact seven-day counts;
        # ProfileStore still owns thresholds and the anomaly decision.
        prefix = ".".join(safe_subject.split(".")[:3]) + "."
        cutoff = candidate.at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            group_rows = _q(
                "SELECT srcip, count() AS sessions, uniqExactIf(dstip, dstip!='') AS peers, "
                "countIf(action='accept') AS accepted, countIf(action='deny') AS denied "
                f"FROM {_CH_DB}.facts WHERE startsWith(srcip,'{prefix}') "
                f"AND event_ts < toDateTime64('{cutoff}',3) "
                f"AND event_ts >= toDateTime64('{cutoff}',3) - INTERVAL 7 DAY "
                "GROUP BY srcip ORDER BY srcip LIMIT 256"
            )
        except Exception:  # self-history anomalies remain useful without a cohort query
            group_rows = []
        known_candidate_peer = (
            (str(candidate.dst_ip),)
            if candidate.dst_ip is not None
            and any(item.dst_ip == candidate.dst_ip for item in events)
            else ()
        )
        for row in group_rows:
            row_ip = str(row.get("srcip") or "")
            if not row_ip:
                continue
            profile.seed_group_summary(
                row_ip,
                sessions=int(row.get("sessions") or 0),
                peer_count=int(row.get("peers") or 0),
                accepted=int(row.get("accepted") or 0),
                denied=int(row.get("denied") or 0),
                known_peers=known_candidate_peer if row_ip == safe_subject else (),
            )
        return list(dict.fromkeys(item.type for item in profile.anomalies(candidate)))
    except Exception:  # noqa: BLE001 - portrait loss must preserve the old order
        return []

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

_READONLY_ADAPTER_PROBES = frozenset({
    "adapter:fortigate_context",
    "adapter:case_flow_window",
    "adapter:device_history",
    "adapter:live_flows",
    "adapter:environment_finding",
    "adapter:admin_auth_window",
})


def _is_safe_probe(command: str) -> bool:
    return command.strip() in _READONLY_ADAPTER_PROBES or is_safe(command)


def _diagnostic_signal_family(command: str) -> str | None:
    """Name the independent signal a proposed open-root check can establish.

    Generic safe commands such as ``date`` and ``uptime`` remain useful reads,
    but they cannot confirm an arbitrary root cause. Open-root confirmation is
    limited to tools whose output has a direct operational meaning here.
    """
    command = command.strip()
    if command == "adapter:case_flow_window":
        return "flow_window"
    if command == "adapter:fortigate_context":
        return "device_configuration"
    if command == "adapter:environment_finding":
        return "address_ownership_ledger"
    if command == "adapter:admin_auth_window":
        return "gateway_auth_events"
    if command in {"adapter:device_history", "adapter:live_flows"}:
        return "device_telemetry"
    prefixes = (
        ("ip -br link show", "link_state"),
        ("ip -br addr show", "address_state"),
        ("ip route show", "route_state"),
        ("ip neigh show", "neighbor_state"),
        ("arp -n", "neighbor_state"),
        ("systemctl --failed", "service_state"),
        ("systemctl status", "service_state"),
        ("systemctl show", "service_state"),
        ("journalctl", "service_logs"),
        ("dmesg", "kernel_logs"),
        ("df -h", "filesystem_state"),
        ("free -m", "memory_state"),
        ("ss -tulpn", "listener_state"),
        ("curl -s", "endpoint_state"),
        ("ping -c", "path_reachability"),
    )
    return next((family for prefix, family in prefixes if command.startswith(prefix)), None)


def _execute_readonly_probe(session: "Session", command: str) -> dict[str, Any]:
    """Execute one closed adapter name or delegate to the shell allowlist."""
    command = command.strip()
    if command not in _READONLY_ADAPTER_PROBES:
        return run(command).as_evidence(session.next_evidence_id())
    try:
        if command == "adapter:fortigate_context":
            from .investigation_tools import collect_fortigate_context

            payload = collect_fortigate_context(session.subject)
            ok = not bool(payload.get("degraded"))
        elif command == "adapter:environment_finding":
            from .investigation_tools import collect_environment_finding

            payload = collect_environment_finding(
                str(session.incident_facts.get("environmentFindingId") or ""),
                session.subject,
            )
            ok = bool(payload.get("available"))
        elif command == "adapter:admin_auth_window":
            from .investigation_tools import collect_admin_auth_window

            payload = collect_admin_auth_window(
                session.incident_start,
                session.incident_end,
                managed_device=str(session.incident_facts.get("managedDevice") or ""),
                failure_threshold=int(session.incident_facts.get("threshold") or 12),
                distinct_source_threshold=int(
                    session.incident_facts.get("distinctSourceThreshold") or 5
                ),
            )
            ok = bool(payload.get("available"))
        elif command == "adapter:case_flow_window":
            from .investigation_tools import collect_case_flow_window

            payload = collect_case_flow_window(
                session.incident_facts,
                session.incident_start,
                session.incident_end,
            )
            ok = bool(payload.get("available"))
        elif command == "adapter:device_history":
            from .history import device_history

            payload = device_history(session.subject or "", 7, False) or {}
            ok = bool(payload.get("ok", True))
        else:
            from .live_identity import live_flows

            payload = live_flows(session.subject or "") or {}
            ok = bool(payload.get("ok", True))
        return {
            "evidence_id": session.next_evidence_id(),
            "command": command,
            "output": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "ok": ok,
            "exit_code": 0 if ok else 1,
            "source": "live_tool",
        }
    except Exception as error:  # adapter failures stay visible as failed observations
        return {
            "evidence_id": session.next_evidence_id(),
            "command": command,
            "output": f"{type(error).__name__}: {error}"[:800],
            "ok": False,
            "exit_code": 1,
            "source": "live_tool",
        }


@dataclass
class Session:
    session_id: str
    question: str
    family: str | None
    subject: str | None
    case_id: str | None = None
    asset_ids: list[str] = field(default_factory=list)
    external_actors: list[str] = field(default_factory=list)
    incident_start: str | None = None
    incident_end: str | None = None
    fault_domain: str | None = None
    scope_quality: str = "unresolved"
    scope_basis: list[str] = field(default_factory=list)
    scope_missing: list[str] = field(default_factory=list)
    allowed_sources: list[str] = field(default_factory=list)
    device_versions: list[str] = field(default_factory=list)
    asset_versions: dict[str, str] = field(default_factory=dict)
    source_refs: list[dict[str, str]] = field(default_factory=list)
    incident_facts: dict[str, Any] = field(default_factory=dict)
    auto_started: bool = False
    memory_enabled: bool = True
    evaluation_source_case_id: str | None = None
    evaluation_strategy: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    runbook: list[dict[str, Any]] = field(default_factory=list)
    # Candidate probes include the unexecuted tail after an evidence-confirmed
    # early stop.  Keeping that tail visible is what makes "reordered" auditable
    # as a permutation instead of looking like memory silently deleted checks.
    probe_candidates: list[str] = field(default_factory=list)
    probe_prior: dict[str, Any] = field(default_factory=dict)
    historical_context: dict[str, Any] = field(default_factory=dict)
    knowledge_context: list[dict[str, Any]] = field(default_factory=list)
    # One normalized receipt across indexed memories, operational history and
    # reference knowledge.  The raw source-shaped fields above remain for the
    # existing UI, while this list is what the model and trace ledger consume.
    retrieval_results: list[dict[str, Any]] = field(default_factory=list)
    # Durable state of competing causes and the probes that separated them.
    # This snapshot is restored with the rest of the session after a restart.
    hypothesis_state: dict[str, Any] = field(default_factory=dict)
    probe_rounds: list[dict[str, Any]] = field(default_factory=list)
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: str = ""
    root_cause: str = ""
    analysis_citations: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
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
        _prepare_probe_for_command(self, command)
        item = _execute_readonly_probe(self, command)
        item["at"] = datetime.now(timezone.utc).isoformat()
        self.evidence.append(item)
        _record_probe_observation(self, item)
        _after_evidence(self, item)
        return item

    def collect_observation(
        self,
        *,
        label: str,
        payload: Mapping[str, Any],
        ok: bool = True,
        observed_at: str | None = None,
        source: str = "read_only_adapter",
    ) -> dict[str, Any]:
        """File a read-only adapter response in the same citable evidence ledger."""
        if len(self.evidence) >= MAX_EVIDENCE:
            return {"evidence_id": "", "command": label, "output": "", "ok": False,
                    "refused": "session evidence limit reached"}
        item = {
            "evidence_id": self.next_evidence_id(),
            "command": label,
            "output": json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
            "ok": bool(ok),
            "at": observed_at or datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
        self.evidence.append(item)
        _after_evidence(self, item)
        return item

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "question": self.question,
            "family": self.family,
            "subject": self.subject,
            "case_id": self.case_id,
            "asset_ids": self.asset_ids,
            "external_actors": self.external_actors,
            "incident_start": self.incident_start,
            "incident_end": self.incident_end,
            "fault_domain": self.fault_domain,
            "scope_quality": self.scope_quality,
            "scope_basis": self.scope_basis,
            "scope_missing": self.scope_missing,
            "allowed_sources": self.allowed_sources,
            "device_versions": self.device_versions,
            "asset_versions": self.asset_versions,
            "source_refs": self.source_refs,
            "incident_facts": self.incident_facts,
            "auto_started": self.auto_started,
            "memory_enabled": self.memory_enabled,
            "evaluation_source_case_id": self.evaluation_source_case_id,
            "evaluation_strategy": self.evaluation_strategy,
            "evidence": self.evidence,
            "turns": self.turns,
            "runbook": self.runbook,
            "probe_candidates": self.probe_candidates,
            "probe_prior": self.probe_prior,
            "historical_context": self.historical_context,
            "knowledge_context": self.knowledge_context,
            "retrieval_results": self.retrieval_results,
            "hypothesis_state": self.hypothesis_state,
            "probe_rounds": self.probe_rounds,
            "trace_events": self.trace_events,
            "diagnosis": self.diagnosis,
            "root_cause": self.root_cause,
            "analysis_citations": self.analysis_citations,
            "decision": self.decision,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Session":
        return cls(
            session_id=str(value["session_id"]),
            question=str(value.get("question") or ""),
            family=value.get("family"),
            subject=value.get("subject"),
            case_id=value.get("case_id"),
            asset_ids=list(value.get("asset_ids") or ()),
            external_actors=list(value.get("external_actors") or ()),
            incident_start=value.get("incident_start"),
            incident_end=value.get("incident_end"),
            fault_domain=value.get("fault_domain"),
            scope_quality=str(value.get("scope_quality") or "unresolved"),
            scope_basis=list(value.get("scope_basis") or ()),
            scope_missing=list(value.get("scope_missing") or ()),
            allowed_sources=list(value.get("allowed_sources") or ()),
            device_versions=list(value.get("device_versions") or ()),
            asset_versions=dict(value.get("asset_versions") or {}),
            source_refs=list(value.get("source_refs") or ()),
            incident_facts=dict(value.get("incident_facts") or {}),
            auto_started=bool(value.get("auto_started", False)),
            memory_enabled=bool(value.get("memory_enabled", True)),
            evaluation_source_case_id=value.get("evaluation_source_case_id"),
            evaluation_strategy=value.get("evaluation_strategy"),
            evidence=list(value.get("evidence") or ()),
            turns=list(value.get("turns") or ()),
            runbook=list(value.get("runbook") or ()),
            probe_candidates=list(value.get("probe_candidates") or ()),
            probe_prior=dict(value.get("probe_prior") or {}),
            historical_context=dict(value.get("historical_context") or {}),
            knowledge_context=list(value.get("knowledge_context") or ()),
            retrieval_results=list(value.get("retrieval_results") or ()),
            hypothesis_state=dict(value.get("hypothesis_state") or {}),
            probe_rounds=list(value.get("probe_rounds") or ()),
            trace_events=list(value.get("trace_events") or ()),
            diagnosis=str(value.get("diagnosis") or ""),
            root_cause=str(value.get("root_cause") or ""),
            analysis_citations=list(value.get("analysis_citations") or ()),
            decision=dict(value.get("decision") or {}),
            opened_at=str(value.get("opened_at") or datetime.now(timezone.utc).isoformat()),
        )


_SESSIONS: dict[str, Session] = {}


def _session_store():
    from .config import Settings
    from .investigation_session_store import InvestigationSessionStore

    return InvestigationSessionStore(Settings.from_env().investigation_session_store_dir)


def _persist_session(session: Session) -> None:
    _session_store().save(session.as_dict())


def _append_case_event(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    event_id: str,
    status: str | None = None,
) -> None:
    if not session.case_id:
        return
    try:
        from . import main

        main._case_repository().append_event(
            session.case_id,
            kind=kind,
            payload=payload,
            event_id=event_id,
            status=status,
        )
    except Exception as error:  # case history degradation stays visible in the session
        session.trace_events.append({
            "kind": "case_history_write_failed",
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": {"event": kind, "error": f"{type(error).__name__}: {error}"[:240]},
        })


def _after_evidence(session: Session, item: dict[str, Any]) -> None:
    evidence_id = str(item.get("evidence_id") or "")
    if evidence_id:
        _append_case_event(
            session,
            "evidence_collected",
            {
                "sessionId": session.session_id,
                "evidenceId": evidence_id,
                "probe": str(item.get("command") or "")[:240],
                "ok": bool(item.get("ok")),
            },
            event_id=f"{session.session_id}:evidence:{evidence_id}",
            status="investigating",
        )
    _persist_session(session)


def _live_memory_store() -> TieredMemoryStore | None:
    """Use the gateway service's long-lived store; offline imports degrade empty."""
    try:
        from . import main

        service = getattr(main, "_evolving_service", None)
        memory = getattr(service, "memory", None)
        return memory if isinstance(memory, TieredMemoryStore) else None
    except Exception:  # noqa: BLE001 - startup/degraded mode must retain old probing
        return None


def _operational_context(subject: str | None, family: str | None) -> dict[str, Any]:
    """Recall domain records as hypotheses; absence never changes probe coverage."""
    try:
        from . import main

        service = getattr(main, "_operational_memory", None)
        if service is None:
            return {}
        return dict(service.recall(subject=subject, family=family, limit=6))
    except Exception:  # source degradation must preserve evidence collection
        return {}


def _query_terms(question: str, family: str | None, subject: str | None) -> list[str]:
    # The subject is already passed through the exact asset/entity route.  Its
    # syntactic suffix must not become a causal hint: every ``*.service`` asset
    # would otherwise boost ``service_failed`` before any observation exists.
    terms = tokenize(" ".join(item for item in (question, family or "") if item))
    for marker, additions in _QUERY_BRIDGE.items():
        if marker in question:
            terms.extend(additions)
    return list(dict.fromkeys(terms))


def _root_keys(record: MemoryRecord) -> list[str]:
    return [tag[len("root:"):] for tag in record.tags if tag.startswith("root:") and len(tag) > 5]


def _memory_eligible_probes() -> set[str]:
    return {
        *TRIAGE_PROBES,
        *(spec.probe for spec in _ACTIVE_ROOTS.values()),
    }


def _record_probes(record: MemoryRecord) -> tuple[list[str], list[str]]:
    """Resolve only registered read-only probes, preserving the declared order."""
    commands: list[str] = []
    skills: list[str] = []
    eligible = _memory_eligible_probes()
    for tag in record.tags:
        if tag.startswith("probe:"):
            command = tag[len("probe:"):]
            if command in eligible:
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
    *,
    as_of: datetime | None = None,
    candidate_probes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return a full triage permutation plus the memories that earned its prefix.

    Retrieval may surface old records for inspection, but a fully stale record has
    zero routing weight.  Partly stale records keep a proportionally smaller vote;
    this uses the shared lifecycle contract rather than inventing a second clock.
    """
    catalogue = list(candidate_probes or TRIAGE_PROBES)
    empty = {
        "ordered": catalogue,
        "preferred": [],
        "skills": [],
        "memory_ids": [],
        "root_key": None,
        "procedural_confidence": 0.0,
        "considered": [],
        "retrieval_results": [],
        "strictly_narrowed": False,
    }
    if memory is None:
        return empty

    terms = _query_terms(question, family, subject)
    assets = [subject] if subject else []
    recalled = memory.retrieve(
        terms,
        assets,
        limit_per_tier=8,
        graph_depth=1,
        as_of=as_of,
    )
    diagnostics = {
        str(item.get("memory_id")): item
        for item in memory.retrieval_diagnostics()
        if item.get("memory_id")
    }
    retrieval_results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tier, records in recalled.items():
        for source_rank, record in enumerate(records, start=1):
            if record.memory_id in seen_ids:
                continue
            seen_ids.add(record.memory_id)
            detail = dict(diagnostics.get(record.memory_id) or {})
            searchable = tokenize(" ".join([record.text, *record.tags, *record.asset_ids]))
            matched_terms = sorted(set(terms).intersection(searchable))
            matched_on: list[str] = []
            if matched_terms:
                matched_on.append("query_terms")
            if subject and subject in record.asset_ids:
                matched_on.append("subject")
            if family and any(family == tag or family in tag for tag in record.tags):
                matched_on.append("family")
            if int(detail.get("graph_hop") or 0) > 0:
                matched_on.append("relation_graph")
            routes = [
                name
                for name, active in (
                    ("bm25", float(detail.get("lexical_score") or 0.0) > 0.0),
                    ("asset_exact", int(detail.get("asset_hits") or 0) > 0),
                    ("entity_exact", bool(detail.get("entity_hits"))),
                    ("vector", float(detail.get("vector_score") or 0.0) > 0.0),
                    ("relation_graph", int(detail.get("graph_hop") or 0) > 0),
                )
                if active
            ]
            retrieval_results.append({
                "kind": "indexed_memory",
                "item_id": record.memory_id,
                "tier": tier,
                "summary": record.text[:800],
                "source": "tiered_memory_store",
                "locator": f"memory:{record.memory_id}",
                "route": routes or ["memory_index"],
                "score": float(detail.get("final_score") or 0.0),
                "score_type": "memory_hybrid_final",
                "score_components": {
                    key: detail.get(key)
                    for key in (
                        "lexical_score", "asset_hits", "entity_hits", "entity_score",
                        "vector_score", "graph_hop", "graph_parent_id",
                        "structural_prior", "final_score",
                    )
                    if key in detail
                },
                "source_rank": source_rank,
                "matched_terms": matched_terms,
                "matched_on": matched_on,
                "source_trace_ids": list(record.source_trace_ids),
                "asset_ids": list(record.asset_ids),
                "observed_at": (
                    record.last_observed_at.isoformat()
                    if record.last_observed_at is not None else None
                ),
                "valid_from": (
                    record.valid_from.isoformat() if record.valid_from is not None else None
                ),
                "valid_to": (
                    record.valid_to.isoformat() if record.valid_to is not None else None
                ),
                "applicable_versions": [record.config_version] if record.config_version else [],
                "historical_only": True,
                "selected_for_context": True,
            })
    procedural = list(recalled.get("procedural", []))
    semantic = list(recalled.get("semantic", []))
    if not procedural and not semantic:
        return {**empty, "retrieval_results": retrieval_results}

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
        exact_asset = not subject or subject in procedure.asset_ids
        same_family = not family or family in procedure.tags
        verified_strength = float(procedure.confidence) >= 1.5
        if (
            proc_weight <= 0.0
            or proc_stale >= 1.0
            or not exact_asset
            or not same_family
            or not verified_strength
        ):
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
        return {
            **empty,
            "considered": considered,
            "retrieval_results": retrieval_results,
        }

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
    preferred = [command for command in preferred if command in catalogue]
    strictly_narrowed = bool(preferred) and set(preferred) < set(catalogue)
    if not strictly_narrowed:
        return {
            **empty,
            "considered": considered,
            "retrieval_results": retrieval_results,
        }
    ordered = preferred + [command for command in catalogue if command not in preferred]
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
        "retrieval_results": retrieval_results,
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
        line.split(maxsplit=1)[0] == subject and re.search(r"\bFAILED\b", line)
        for line in neighbours.splitlines()
        if line.split()
    ):
        confirmed.add("neighbor_unreachable")

    failed = _output_for(evidence, "systemctl --failed --no-legend")
    if failed is not None and failed.strip():
        confirmed.add("service_failed")
        if subject and subject.endswith(".service") and subject in failed:
            confirmed.add("sentinel.failed_units")

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


def _loop_for(session: Session) -> HypothesisLoop | None:
    if not session.hypothesis_state:
        return None
    return HypothesisLoop.restore(session.hypothesis_state)


def _store_loop(session: Session, loop: HypothesisLoop) -> None:
    session.hypothesis_state = loop.state.model_dump(mode="json")


def _initialise_hypothesis_loop(
    session: Session,
    ordered_commands: list[str],
) -> HypothesisLoop:
    opened = datetime.fromisoformat(session.opened_at.replace("Z", "+00:00"))
    loop = create_network_hypothesis_loop(
        case_id=session.case_id or session.session_id,
        family=session.family,
        subject=session.subject,
        opened_at=opened,
        question_terms=(
            []
            if session.evaluation_strategy == "fixed_script"
            else _query_terms(session.question, session.family, session.subject)
        ),
        ordered_commands=ordered_commands,
        preferred_root_ids=(
            [str(session.probe_prior.get("root_key"))]
            if session.probe_prior.get("strictly_narrowed")
            and session.probe_prior.get("root_key")
            else []
        ),
    )
    _store_loop(session, loop)
    for hypothesis in loop.state.hypotheses:
        _persist_session_trace(session, "hypothesis_proposed", {
            "session_id": session.session_id,
            "case_id": session.case_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "entity_id": hypothesis.entity_id,
            "statement": hypothesis.statement,
            "state_version": loop.state.state_version,
        })
    return loop


def _record_probe_observation(session: Session, item: Mapping[str, Any]) -> None:
    loop = _loop_for(session)
    if loop is None:
        return
    command = str(item.get("command") or "")
    selected = next(
        (
            probe
            for probe in loop.state.probes
            if probe.status == "selected" and _command_matches_probe(command, probe.description)
        ),
        None,
    )
    if selected is None:
        return
    root_id = selected.distinguishes_hypothesis_ids[0]
    if selected.observation_predicate is not None:
        matched = evaluate_observation(
            selected.observation_predicate,
            output=str(item.get("output") or ""),
            ok=bool(item.get("ok")),
        )
        if matched is None:
            polarity, decisive, collection_status = "neutral", False, "tool_failed"
        else:
            polarity, decisive, collection_status = (
                ("supports", True, "observed")
                if matched
                else ("opposes", True, "observed")
            )
        if isinstance(item, dict):
            item["claim_support"] = {
                "hypothesisId": root_id,
                "signalFamily": _diagnostic_signal_family(command),
                "operator": selected.observation_predicate.operator,
                "expected": selected.observation_predicate.value,
                "matched": matched,
                "frozenBeforeProbe": True,
            }
    else:
        polarity, decisive, collection_status = _probe_polarity(
            root_id, item, session.subject
        )
    observed_at = datetime.fromisoformat(
        str(item.get("at") or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00")
    )
    observation = loop.record_evidence(EvidenceInput(
        evidence_id=str(item.get("evidence_id")),
        hypothesis_id=root_id,
        entity_id=session.subject or "local-system",
        observed_at=observed_at,
        source="live_tool",
        polarity=polarity,
        decisive=decisive,
        collection_status=collection_status,
        summary=(
            f"{command}: predicate "
            f"{selected.observation_predicate.operator} "
            f"{selected.observation_predicate.value!r} -> {matched}"
            if selected.observation_predicate is not None
            else f"{command}: {'succeeded' if item.get('ok') else 'failed'}"
        ),
        probe_id=selected.probe_id,
    ))
    _store_loop(session, loop)
    hypothesis = loop.get_hypothesis(root_id)
    event_kind = {
        "proposed": "hypothesis_proposed",
        "testing": "hypothesis_testing",
        "rejected": "hypothesis_rejected",
        "confirmed": "hypothesis_confirmed",
    }[hypothesis.status]
    _persist_session_trace(session, event_kind, {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "hypothesis_id": root_id,
        "status": hypothesis.status,
        "probe_id": selected.probe_id,
        "evidence_id": observation.evidence_id,
        "polarity": observation.polarity,
        "decisive": observation.decisive,
        "collection_status": observation.collection_status,
        "state_version": loop.state.state_version,
    })


def _prepare_probe_for_command(session: Session, command: str) -> None:
    """Attach a model-requested command to its available hypothesis probe."""
    loop = _loop_for(session)
    if loop is None:
        return
    if any(
        probe.status == "selected" and _command_matches_probe(command, probe.description)
        for probe in loop.state.probes
    ):
        return
    available = next(
        (
            probe for probe in loop.state.probes
            if probe.status == "available" and _command_matches_probe(command, probe.description)
        ),
        None,
    )
    if available is None:
        return
    try:
        selected = loop.select_probe(available.probe_id)
    except ValueError:
        return
    _store_loop(session, loop)
    _persist_session_trace(session, "discriminating_probe_selected", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "probe_id": selected.probe_id,
        "command": selected.description,
        "distinguishes": list(selected.distinguishes_hypothesis_ids),
        "selection_source": "requested_command",
        "state_version": loop.state.state_version,
    })


def _run_next_discriminating_probe(session: Session) -> dict[str, Any] | None:
    loop = _loop_for(session)
    if loop is None:
        return None
    selected = loop.select_next_probe()
    if selected is None:
        return None
    _store_loop(session, loop)
    _persist_session_trace(session, "discriminating_probe_selected", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "probe_id": selected.probe_id,
        "command": selected.description,
        "distinguishes": list(selected.distinguishes_hypothesis_ids),
        "state_version": loop.state.state_version,
    })
    item = session.collect(selected.description)
    session.probe_rounds.append({
        "probe_id": selected.probe_id,
        "command": selected.description,
        "evidence_id": item.get("evidence_id"),
        "ok": bool(item.get("ok")),
        "at": item.get("at"),
    })
    return item


def _hypothesis_view(session: Session) -> dict[str, Any]:
    loop = _loop_for(session)
    if loop is None:
        return {"state_version": 0, "hypotheses": [], "probes": []}
    state = loop.state
    return {
        "state_version": state.state_version,
        "hypotheses": [item.model_dump(mode="json") for item in state.hypotheses],
        "probes": [item.model_dump(mode="json") for item in state.probes],
        "confirmed_root_keys": [
            item.hypothesis_id for item in state.hypotheses if item.status == "confirmed"
        ],
        "active_root_keys": [
            item.hypothesis_id for item in state.hypotheses if item.status in {"proposed", "testing"}
        ],
    }


def _register_model_hypothesis(
    session: Session,
    payload: Mapping[str, Any],
) -> str | None:
    """Freeze an open root and its falsifiable checks before executing them.

    A model-origin root needs two different safe observations.  The predicates
    are stored in the aggregate before the command runs; later output is judged
    by the stored boolean checks rather than by another model interpretation.
    """
    statement = re.sub(
        r"\[\s*ev-[0-9A-Za-z_-]+\s*\]",
        "",
        str(payload.get("root_cause") or ""),
        flags=re.IGNORECASE,
    )
    statement = re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", " ".join(statement.split())).strip()
    if not statement or statement.casefold() == "inconclusive":
        return None
    loop = _loop_for(session)
    if loop is None:
        return None
    requested_id = str(payload.get("root_hypothesis_id") or "").strip()
    existing = next(
        (
            item
            for item in loop.state.hypotheses
            if item.hypothesis_id == requested_id
            or item.statement.casefold() == statement.casefold()
        ),
        None,
    )
    if existing is not None:
        if existing.origin == "model":
            existing_commands = {
                item.description
                for item in loop.state.probes
                if existing.hypothesis_id in item.distinguishes_hypothesis_ids
            }
            next_index = len(existing_commands) + 1
            raw_checks = payload.get("verification") or ()
            proposed_checks: list[tuple[str, ObservationPredicate]] = []
            for raw in raw_checks[:3] if isinstance(raw_checks, list) else ():
                if not isinstance(raw, Mapping):
                    continue
                command = str(raw.get("command") or "").strip()
                if (
                    not command
                    or command in existing_commands
                    or not _is_safe_probe(command)
                    or _diagnostic_signal_family(command) is None
                ):
                    continue
                try:
                    predicate = ObservationPredicate.model_validate({
                        "operator": raw.get("operator"),
                        "value": raw.get("value"),
                        "case_sensitive": bool(raw.get("case_sensitive", False)),
                    })
                except (TypeError, ValueError):
                    continue
                proposed_checks.append((command, predicate))
            combined_families = {
                _diagnostic_signal_family(command)
                for command in [
                    *existing_commands,
                    *(command for command, _ in proposed_checks),
                ]
            }
            if len({value for value in combined_families if value is not None}) < 2:
                proposed_checks = []
            for command, predicate in proposed_checks:
                loop.add_probe(ProbeCandidate(
                    probe_id=f"probe:{existing.hypothesis_id}:{next_index}",
                    description=command,
                    target_entity_id=session.subject or "local-system",
                    distinguishes_hypothesis_ids=(existing.hypothesis_id,),
                    priority=20_000 - next_index,
                    estimated_cost=1.0,
                    observation_predicate=predicate,
                ), at=datetime.now(timezone.utc))
                existing_commands.add(command)
                next_index += 1
            _store_loop(session, loop)
        return existing.hypothesis_id
    model_hypotheses = [
        item for item in loop.state.hypotheses if item.hypothesis_id.startswith("model:")
    ]
    if len(model_hypotheses) >= 3:
        return None
    digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()[:12]
    hypothesis_id = f"model:{digest}"
    opened = datetime.fromisoformat(session.opened_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    raw_checks = payload.get("verification") or ()
    checks: list[tuple[str, ObservationPredicate]] = []
    seen_commands: set[str] = set()
    for raw in raw_checks[:3] if isinstance(raw_checks, list) else ():
        if not isinstance(raw, Mapping):
            continue
        command = str(raw.get("command") or "").strip()
        if (
            not command
            or command in seen_commands
            or not _is_safe_probe(command)
            or _diagnostic_signal_family(command) is None
        ):
            continue
        try:
            predicate = ObservationPredicate.model_validate({
                "operator": raw.get("operator"),
                "value": raw.get("value"),
                "case_sensitive": bool(raw.get("case_sensitive", False)),
            })
        except (TypeError, ValueError):
            continue
        checks.append((command, predicate))
        seen_commands.add(command)
    signal_families = {_diagnostic_signal_family(command) for command, _ in checks}
    verification_eligible = len(checks) >= 2 and len(signal_families) >= 2
    loop.add_hypothesis(RootCauseHypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        entity_id=session.subject or "local-system",
        valid_from=opened,
        valid_to=opened + timedelta(days=30),
        updated_at=now,
        required_decisive_supports=max(2, len(checks)),
        origin="model",
        archive_eligible=True,
    ))
    existing_commands = {item.description for item in loop.state.probes}
    for index, (command, predicate) in enumerate(
        checks if verification_eligible else (), start=1
    ):
        if command in existing_commands:
            continue
        loop.add_probe(ProbeCandidate(
            probe_id=f"probe:{hypothesis_id}:{index}",
            description=command,
            target_entity_id=session.subject or "local-system",
            distinguishes_hypothesis_ids=(hypothesis_id,),
            priority=20_000 - index,
            estimated_cost=1.0,
            observation_predicate=predicate,
        ), at=now)
        existing_commands.add(command)
    _store_loop(session, loop)
    _persist_session_trace(session, "hypothesis_proposed", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "hypothesis_id": hypothesis_id,
        "entity_id": session.subject or "local-system",
        "statement": statement,
        "origin": "model_analysis",
        "confirmation_eligible": verification_eligible,
        "verification_count": len(checks),
        "verification_signal_families": sorted(
            family for family in signal_families if family is not None
        ),
        "state_version": loop.state.state_version,
    })
    return hypothesis_id


def _persist_session_trace(
    session: Session,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """Keep an offline-visible receipt and append it to the service ledger."""
    row = {
        "kind": kind,
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
                kind=kind,
                payload=payload,
            ))
    except Exception as error:  # noqa: BLE001 - evidence collection must survive trace degradation
        row["persistence_error"] = f"{type(error).__name__}: {error}"[:240]


def _persist_shortcut_trace(session: Session, payload: dict[str, Any]) -> None:
    """Record a measured probe-order or early-stop effect from recalled memory."""
    _persist_session_trace(session, "memory_shortcut", payload)


def _retrieval_relation(
    session: Session,
    *,
    matched_on: list[str] | tuple[str, ...] = (),
    matched_terms: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Attach every hit to the investigation that requested it."""
    return {
        "investigation_id": session.session_id,
        "subject": session.subject,
        "family": session.family,
        "matched_on": list(matched_on),
        "matched_terms": list(matched_terms),
    }


def _operational_retrieval_results(
    session: Session,
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize source-ranked dossiers, risks and promoted features."""
    results: list[dict[str, Any]] = []
    for key, kind in (
        ("dossiers", "incident_dossier"),
        ("risks", "risk_pattern"),
        ("features", "network_feature"),
    ):
        rows = context.get(key) or ()
        if not isinstance(rows, (list, tuple)):
            continue
        for source_rank, raw in enumerate(rows, start=1):
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            matched_on = [str(item) for item in row.get("matched_on") or ()]
            feature = dict(row.get("feature") or {}) if kind == "network_feature" else row
            if session.subject:
                searchable_subjects = {
                    str(item)
                    for item in (
                        feature.get("subject"), feature.get("asset"), feature.get("scope_key"),
                        *(feature.get("asset_ids") or ()), *(feature.get("affected_assets") or ()),
                        *(feature.get("target_assets") or ()),
                    )
                    if item
                }
                scope = feature.get("scope")
                if isinstance(scope, Mapping):
                    searchable_subjects.update(str(item) for item in scope.get("asset_ids") or ())
                if session.subject in searchable_subjects and "subject" not in matched_on:
                    matched_on.append("subject")
            scope = feature.get("scope")
            scope_family = scope.get("fault_family") if isinstance(scope, Mapping) else None
            family_value = str(
                feature.get("family") or feature.get("fault_family") or scope_family or ""
            )
            if session.family and family_value == session.family and "family" not in matched_on:
                matched_on.append("family")
            item_id = str(
                feature.get("dossier_id") or feature.get("incident_id")
                or feature.get("pattern_id") or feature.get("feature_id")
                or feature.get("id") or f"{kind}:{source_rank}"
            )
            summary = str(
                feature.get("fault_summary") or feature.get("summary")
                or feature.get("statement") or feature.get("risk_type")
                or feature.get("root_cause") or item_id
            )
            native_score = row.get("score") if kind == "network_feature" else None
            score = float(native_score) if isinstance(native_score, (int, float)) else 1.0 / source_rank
            score_type = (
                "feature_confidence_plus_scope_specificity"
                if native_score is not None
                else "source_rank_reciprocal"
            )
            source = str(
                feature.get("source_mode") or feature.get("provenance")
                or feature.get("source") or "operational_memory"
            )
            asset_ids = sorted({
                str(item)
                for item in (
                    feature.get("subject"), feature.get("asset"),
                    *(feature.get("asset_ids") or ()),
                    *(feature.get("affected_assets") or ()),
                    *(feature.get("target_assets") or ()),
                )
                if item
            })
            if isinstance(scope, Mapping):
                asset_ids = sorted(set(asset_ids).union(
                    str(item) for item in scope.get("asset_ids") or () if item
                ))
            results.append({
                "kind": kind,
                "item_id": item_id,
                "summary": summary[:800],
                "source": source,
                "locator": f"operational-memory:{kind}:{item_id}",
                "route": "scope_and_recency_rank",
                "score": round(score, 12),
                "score_type": score_type,
                "source_rank": source_rank,
                "matched_on": matched_on,
                "matched_terms": [],
                "asset_ids": asset_ids,
                **{
                    key: feature[key]
                    for key in (
                        "observed_at", "last_observed_at", "updated_at",
                        "opened_at", "created_at", "valid_from", "valid_to",
                    )
                    if feature.get(key)
                },
                "historical_only": True,
                "selected_for_context": True,
                "relation_to_current": _retrieval_relation(
                    session,
                    matched_on=matched_on,
                ),
            })
    return results


def _knowledge_retrieval_results(
    session: Session,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize scored knowledge passages without promoting them to telemetry."""
    results: list[dict[str, Any]] = []
    for source_rank, item in enumerate(documents, start=1):
        matched_terms = [str(term) for term in item.get("matched_terms") or ()]
        results.append({
            "kind": "knowledge_document",
            "item_id": str(item.get("document_id") or f"knowledge:{source_rank}"),
            "summary": str(item.get("text") or "")[:800],
            "title": str(item.get("title") or item.get("document_id") or "knowledge"),
            "source": str(item.get("source") or "operations_knowledge_base"),
            "locator": str(item.get("locator") or item.get("document_id") or ""),
            "route": str(item.get("route") or "bm25"),
            "score": float(item.get("score") or 0.0),
            "score_type": str(item.get("route") or "bm25"),
            "source_rank": source_rank,
            "matched_on": ["query_terms"] if matched_terms else [],
            "matched_terms": matched_terms,
            "reference_only": True,
            "selected_for_context": True,
            "relation_to_current": _retrieval_relation(
                session,
                matched_on=["query_terms"] if matched_terms else [],
                matched_terms=matched_terms,
            ),
        })
    return results


def _build_retrieval_results(
    session: Session,
    memory_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the one auditable retrieval payload consumed by reasoning."""
    normalized_memory: list[dict[str, Any]] = []
    for raw in memory_results:
        item = dict(raw)
        item["relation_to_current"] = _retrieval_relation(
            session,
            matched_on=item.get("matched_on") or (),
            matched_terms=item.get("matched_terms") or (),
        )
        normalized_memory.append(item)
    results = [
        *normalized_memory,
        *_operational_retrieval_results(session, session.historical_context),
        *_knowledge_retrieval_results(session, session.knowledge_context),
    ]
    # Keep every hit in the receipt, then mark a bounded, source-balanced subset
    # for the model context. Scores from different indexes are not comparable, so
    # selection is per tier/kind instead of sorting all sources into one fake scale.
    selected_per_group: dict[str, int] = {}
    for item in results:
        kind = str(item.get("kind") or "unknown")
        group = f"{kind}:{item.get('tier') or ''}"
        limit = 2 if kind in {
            "indexed_memory", "incident_dossier", "risk_pattern", "network_feature",
        } else 4
        selected = selected_per_group.get(group, 0) < limit
        item["selected_for_context"] = selected
        if selected:
            selected_per_group[group] = selected_per_group.get(group, 0) + 1
    return results


def _candidate_time(item: Mapping[str, Any]) -> datetime | None:
    """Read a source timestamp when one is present, without inventing one."""
    for key in (
        "observed_at", "last_observed_at", "updated_at", "opened_at", "created_at",
    ):
        raw = item.get(key)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return None


def _constrain_retrieval_results(session: Session) -> None:
    """Filter every recalled item against the current asset and incident window.

    Existing indexes remain responsible for producing candidates.  This step
    owns the boundary between a relevant-looking passage and context that is
    actually applicable to the current investigation.
    """
    candidates: list[EvidenceCandidate] = []
    result_by_evidence_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(session.retrieval_results, start=1):
        kind = str(item.get("kind") or "")
        candidate_kind = (
            "document"
            if kind == "knowledge_document"
            else "historical_incident"
        )
        evidence_id = f"retrieval:{kind}:{item.get('item_id') or index}"
        # Prefixes make identities stable even when two stores use the same id.
        while evidence_id in result_by_evidence_id:
            evidence_id += f":{index}"
        matched_on = {str(value) for value in item.get("matched_on") or ()}
        item_assets = tuple(str(value) for value in item.get("asset_ids") or () if value)
        scoped_to_subject = bool(session.subject and "subject" in matched_on)
        asset_ids = item_assets or (
            (session.subject,) if scoped_to_subject and session.subject else ()
        )
        entity_ids = asset_ids
        candidates.append(EvidenceCandidate(
            evidence_id=evidence_id,
            text=str(item.get("summary") or item.get("title") or ""),
            kind=candidate_kind,
            source=str(item.get("source") or "unknown"),
            asset_ids=asset_ids,
            entity_ids=entity_ids,
            observed_at=_candidate_time(item),
            valid_from=_candidate_time({"observed_at": item.get("valid_from")}),
            valid_to=_candidate_time({"observed_at": item.get("valid_to")}),
            applicable_versions=tuple(
                str(value) for value in item.get("applicable_versions") or () if value
            ),
            upstream_rank=index,
            metadata={
                "kind": kind,
                "item_id": str(item.get("item_id") or ""),
                "locator": str(item.get("locator") or ""),
            },
        ))
        result_by_evidence_id[evidence_id] = item

    if not candidates:
        return
    incident_start = datetime.fromisoformat(
        str(session.incident_start or session.opened_at).replace("Z", "+00:00")
    )
    incident_end = datetime.fromisoformat(
        str(session.incident_end or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00")
    )
    scope = RetrievalScope(
        query_text=" ".join(
            value for value in (session.question, session.family or "", session.subject or "")
            if value
        ),
        asset_ids=tuple(session.asset_ids or ([session.subject] if session.subject else [])),
        incident_start=incident_start,
        incident_end=incident_end,
        allowed_sources=tuple(session.allowed_sources),
        device_versions=tuple(session.device_versions),
        asset_versions=session.asset_versions,
        seed_entities=tuple(session.asset_ids or ([session.subject] if session.subject else [])),
        history_since=incident_start - timedelta(days=180),
        max_relation_hops=2,
    )
    retrieved = EvidenceRetriever(candidates).retrieve(
        scope,
        top_k=min(12, len(candidates)),
    )
    kept_ids = {entry.candidate.evidence_id for entry in retrieved.kept}
    for entry in retrieved.kept:
        item = result_by_evidence_id[entry.candidate.evidence_id]
        item["selected_for_context"] = True
        item["context_rank"] = entry.rank
        item["context_score"] = entry.score
        item["selection_reasons"] = list(entry.reasons)
        item["decisive_for_current_incident"] = entry.decisive_for_current_incident
        item.pop("drop_reasons", None)
    for entry in retrieved.dropped:
        item = result_by_evidence_id[entry.candidate.evidence_id]
        item["selected_for_context"] = False
        item["drop_reasons"] = list(entry.reasons)
        item["decisive_for_current_incident"] = False
        item.pop("selection_reasons", None)

    session.retrieval_results.sort(key=lambda item: (
        not bool(item.get("selected_for_context")),
        int(item.get("context_rank") or 1_000_000),
        str(item.get("kind") or ""),
        str(item.get("item_id") or ""),
    ))
    drop_counts: dict[str, int] = {}
    for entry in retrieved.dropped:
        for reason in entry.reasons:
            drop_counts[reason] = drop_counts.get(reason, 0) + 1
    _persist_session_trace(session, "retrieval_candidates_filtered", {
        "session_id": session.session_id,
        "subject": session.subject,
        "incident_start": incident_start.isoformat(),
        "incident_end": incident_end.isoformat(),
        "candidate_count": len(candidates),
        "kept_count": len(kept_ids),
        "dropped_count": len(retrieved.dropped),
        "drop_counts": drop_counts,
        "kept_ids": sorted(kept_ids),
        "dense_used": retrieved.dense_used,
    })


def _retrieval_context(session: Session) -> list[dict[str, Any]]:
    """Detached bounded retrieval payload supplied to each model call."""
    return [
        dict(item)
        for item in session.retrieval_results
        if item.get("selected_for_context")
    ]


def _session_open_response(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "question": session.question,
        "family": session.family,
        "subject": session.subject,
        "case_id": session.case_id,
        "auto_started": session.auto_started,
        "incident_scope": {
            "asset_ids": session.asset_ids,
            "external_actors": session.external_actors,
            "start": session.incident_start,
            "end": session.incident_end,
            "fault_domain": session.fault_domain,
            "quality": session.scope_quality,
            "basis": session.scope_basis,
            "missing": session.scope_missing,
            "source_refs": session.source_refs,
        },
        "incident_facts": session.incident_facts,
        "evidence": session.evidence,
        "probe_candidates": session.probe_candidates,
        "probe_prior": session.probe_prior,
        "historical_context": session.historical_context,
        "knowledge_context": session.knowledge_context,
        "retrieval_results": session.retrieval_results,
        "hypothesis_state": _hypothesis_view(session),
        "probe_rounds": session.probe_rounds,
        "trace_events": session.trace_events,
        "decision": session.decision or None,
        "summary": _summarise(session),
    }


def start(
    question: str,
    family: str | None = None,
    subject: str | None = None,
    case_id: str | None = None,
    *,
    auto_started: bool = False,
    memory_enabled: bool = True,
    evaluation_only: bool = False,
    evaluation_strategy: str | None = None,
    restart_existing: bool = False,
) -> dict[str, Any]:
    """Open a session and run its opening probes before anything reasons."""
    case = None
    case_scope: dict[str, Any] = {}
    if case_id:
        from . import main
        from .investigation_cases import derive_investigation_scope

        case = main._case_repository().get(case_id)
        if case is None:
            raise ValueError("unknown investigation case")
        case_scope = derive_investigation_scope(case)
        question = question.strip() or str(case_scope["question"])
        family = family or case_scope.get("family")
        subject = subject or case_scope.get("subject")
        latest = case.latest_event("investigation_session_started")
        existing_id = str((latest or {}).get("sessionId") or "")
        if existing_id and not evaluation_only and not restart_existing:
            try:
                existing = get(existing_id)
                refreshed_facts = dict(case_scope.get("incident_facts") or {})
                refreshed_assets = list(case_scope.get("asset_ids") or ())
                became_actionable = bool(
                    existing.scope_quality == "unresolved"
                    and str(case_scope.get("scope_quality") or "unresolved") != "unresolved"
                )
                scope_changed = bool(
                    refreshed_facts != existing.incident_facts
                    or refreshed_assets != existing.asset_ids
                    or case_scope.get("fault_domain") != existing.fault_domain
                    or str(case_scope.get("scope_quality") or "unresolved")
                    != existing.scope_quality
                )
                if scope_changed:
                    existing.incident_facts = refreshed_facts
                    existing.asset_ids = refreshed_assets
                    existing.external_actors = list(case_scope.get("external_actors") or ())
                    existing.incident_start = case_scope.get("incident_start")
                    existing.incident_end = case_scope.get("incident_end")
                    existing.fault_domain = case_scope.get("fault_domain")
                    existing.scope_quality = str(
                        case_scope.get("scope_quality") or existing.scope_quality
                    )
                    existing.scope_basis = list(case_scope.get("scope_basis") or ())
                    existing.scope_missing = list(case_scope.get("scope_missing") or ())
                    existing.source_refs = list(case_scope.get("source_refs") or existing.source_refs)
                    _persist_session(existing)
                if not became_actionable:
                    return _session_open_response(existing)
            except KeyError:
                pass
    session = Session(
        session_id=uuid.uuid4().hex[:12],
        question=question.strip(),
        family=family,
        subject=subject,
        case_id=None if evaluation_only else case_id,
        asset_ids=list(case_scope.get("asset_ids") or ([subject] if subject else [])),
        external_actors=list(case_scope.get("external_actors") or ()),
        incident_start=case_scope.get("incident_start"),
        incident_end=case_scope.get("incident_end"),
        fault_domain=case_scope.get("fault_domain"),
        scope_quality=str(
            case_scope.get("scope_quality") or ("exact" if subject else "unresolved")
        ),
        scope_basis=list(case_scope.get("scope_basis") or ()),
        scope_missing=list(case_scope.get("scope_missing") or ()),
        source_refs=list(case_scope.get("source_refs") or ()),
        incident_facts=dict(case_scope.get("incident_facts") or {}),
        auto_started=auto_started,
        memory_enabled=memory_enabled,
        evaluation_source_case_id=case_id if evaluation_only else None,
        evaluation_strategy=evaluation_strategy if evaluation_only else None,
    )
    _SESSIONS[session.session_id] = session
    _append_case_event(
        session,
        "investigation_session_started",
        {
            "sessionId": session.session_id,
            "question": session.question,
            "subject": session.subject,
            "family": session.family,
            "autoStarted": session.auto_started,
            "scopeQuality": session.scope_quality,
            "faultDomain": session.fault_domain,
            "managedAssets": list(session.asset_ids),
            "externalActors": list(session.external_actors),
        },
        event_id=f"{session.session_id}:started",
        status="investigating",
    )
    _persist_session(session)
    if case is not None:
        session.collect_observation(
            label=f"case:{case.case_id}",
            payload={
                "title": case.title,
                "summary": case.summary,
                "severity": case.severity,
                "rule_id": case.rule_id,
                "service": case.service,
                "scope": case.scope,
                "occurrence_count": case.occurrence_count,
                "incident_facts": session.incident_facts,
                "source_refs": session.source_refs,
            },
            observed_at=case.last_seen_at,
            source="telemetry",
        )
    from domains.network_rca.business_decision import is_terminal_local_in_deny

    terminal_case = bool(
        case is not None
        and family == "fam-policy-reachability"
        and is_terminal_local_in_deny(session.incident_facts)
    )
    case_driven_policy = bool(case is not None and family == "fam-policy-reachability")
    if case is not None and session.scope_quality == "unresolved":
        session.probe_candidates = []
        _persist_session_trace(session, "investigation_scope_unresolved", {
            "session_id": session.session_id,
            "case_id": session.case_id,
            "missing": list(session.scope_missing),
            "source_refs": list(session.source_refs),
        })
        _persist_session(session)
        return _session_open_response(session)
    session.historical_context = (
        _operational_context(subject, family)
        if memory_enabled and not terminal_case and not case_driven_policy
        else {}
    )
    if any(session.historical_context.get(key) for key in ("dossiers", "risks", "features")):
        session.trace_events.append({
            "kind": "operational_memory_recalled",
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "dossiers": len(session.historical_context.get("dossiers") or ()),
                "risks": len(session.historical_context.get("risks") or ()),
                "features": len(session.historical_context.get("features") or ()),
                "historical_only": True,
            },
        })

    query_terms = _query_terms(question, family, subject)
    session.knowledge_context = (
        retrieve_ops_knowledge(question, query_terms=query_terms, limit=4)
        if memory_enabled and not terminal_case and not case_driven_policy
        else []
    )
    if session.knowledge_context:
        session.trace_events.append({
            "kind": "knowledge_retrieved",
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "route": "bm25",
                "document_ids": [
                    item["document_id"] for item in session.knowledge_context
                ],
                "count": len(session.knowledge_context),
                "reference_only": True,
            },
        })

    # A named family gets its targeted checks; an open question gets the full
    # triage sweep, because "what is wrong here" has no smaller honest answer.
    extra = (
        []
        if terminal_case
        else FAMILY_PROBES.get(family or "") or TRIAGE_PROBES
    )
    uses_triage = extra is TRIAGE_PROBES
    # Policy cases establish the exact flow window and current device state first.
    # Other investigations may use verified history to reorder their fixed probes.
    memory_prior = _probe_prior(
        question,
        family,
        subject,
        (
            _live_memory_store()
            if memory_enabled and not terminal_case and not case_driven_policy
            else None
        ),
        as_of=(
            datetime.fromisoformat(session.incident_end.replace("Z", "+00:00"))
            if session.incident_end else None
        ),
        candidate_probes=list(TRIAGE_PROBES if uses_triage else extra),
    )
    if uses_triage:
        prior = memory_prior
    else:
        family_preferred = [
            command for command in memory_prior.get("preferred") or () if command in extra
        ]
        prior = {
            "ordered": [
                *family_preferred,
                *(command for command in extra if command not in family_preferred),
            ],
            "preferred": family_preferred,
            "skills": list(memory_prior.get("skills") or ()) if family_preferred else [],
            "memory_ids": list(memory_prior.get("memory_ids") or ()) if family_preferred else [],
            "root_key": memory_prior.get("root_key") if family_preferred else None,
            "procedural_confidence": (
                memory_prior.get("procedural_confidence", 0.0) if family_preferred else 0.0
            ),
            "considered": list(memory_prior.get("considered") or ()),
            "retrieval_results": list(memory_prior.get("retrieval_results") or ()),
            "strictly_narrowed": bool(family_preferred),
        }
    ordered_extra = list(prior["ordered"])
    profile_anomalies = (
        _device_profile_anomaly_types(subject)
        if uses_triage and session.evaluation_strategy != "fixed_script"
        else []
    )
    # Preserve a procedural memory's earned prefix so its existing evidence-only
    # early-stop contract remains intact.  The portrait sorts the untouched tail;
    # with no procedural prefix it sorts the whole original triage sweep.
    procedural_prefix = list(prior["preferred"])
    profile_tail = [
        command for command in ordered_extra if command not in procedural_prefix
    ]
    ordered_extra = [
        *procedural_prefix,
        *order_triage_by_profile(profile_tail, profile_anomalies),
    ]
    profile_preferred = [
        command
        for command in order_triage_by_profile(profile_tail, profile_anomalies)
        if command in {
            candidate
            for anomaly_type in profile_anomalies
            for candidate in PROFILE_TRIAGE_PROBES.get(anomaly_type, ())
        }
    ]
    if profile_anomalies:
        prior["profile_anomalies"] = profile_anomalies
        prior["profile_preferred"] = profile_preferred
    session.probe_prior = {
        key: value
        for key, value in prior.items()
        if key not in {"ordered", "retrieval_results"}
    }
    session.retrieval_results = _build_retrieval_results(
        session,
        list(memory_prior.get("retrieval_results") or ()),
    )
    _constrain_retrieval_results(session)

    subject_probes: list[str] = []
    unit_subject = bool(
        subject and subject.endswith((".service", ".target", ".socket", ".timer"))
    )
    if (
        not case_driven_policy
        and family != "fam-address-ownership"
        and subject
        and not unit_subject
        and re.fullmatch(r"[\w.:-]{1,64}", subject)
    ):
        for template in (f"ping -c 2 -W 2 {subject}", f"ip neigh show {subject}"):
            if is_safe(template):
                subject_probes.append(template)

    if (
        not case_driven_policy
        and subject
        and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", subject)
    ):
        try:
            from .history import device_history

            history = device_history(subject, 7, False)
            if history is not None:
                session.collect_observation(
                    label=f"clickhouse:device_history {subject} 7d",
                    payload=history,
                    ok=bool(history.get("ok", True)),
                )
        except Exception as error:  # source failure is visible but does not stop host probes
            session.collect_observation(
                label=f"clickhouse:device_history {subject} 7d",
                payload={"error": f"{type(error).__name__}: {error}"[:240]},
                ok=False,
            )

        # Production receives the router credentials through the service
        # environment. Unit tests and developer shells without them do not make
        # a surprise network call or add synthetic evidence.
        configured = bool(
            (os.environ.get("AUTOPOIESIS_FGT_USER") or os.environ.get("FGT_USER"))
            and (os.environ.get("AUTOPOIESIS_FGT_PASS") or os.environ.get("FGT_PASS"))
        )
        if configured:
            try:
                from .investigation_tools import collect_fortigate_context

                router_context = collect_fortigate_context(subject)
                session.collect_observation(
                    label=f"fortigate:network_context {subject}",
                    payload=router_context,
                    ok=not bool(router_context.get("degraded")),
                )
            except Exception as error:
                session.collect_observation(
                    label=f"fortigate:network_context {subject}",
                    payload={"error": f"{type(error).__name__}: {error}"[:240]},
                    ok=False,
                )

    baseline_probes = [] if case_driven_policy else list(BASELINE_PROBES)
    planned = list(baseline_probes)
    for command in [*ordered_extra, *subject_probes]:
        if command not in planned:
            planned.append(command)
    session.probe_candidates = planned
    if not case_driven_policy:
        _initialise_hypothesis_loop(session, ordered_extra)

    for command in baseline_probes:
        session.collect(command)

    # Named fault families keep their bounded diagnostic contract.  Open-ended
    # questions execute a first active slice and retain the untried probes for
    # subsequent evidence rounds.
    preferred = list(prior["preferred"])
    early_stopped = False
    skipped: list[str] = []
    active_budget = (
        len(_FAMILY_ACTIVE_ROOTS.get(family or "", ()))
        if not uses_triage
        else OPENING_ACTIVE_PROBE_BUDGET
    )
    for _index in range(active_budget):
        item = _run_next_discriminating_probe(session)
        if item is None:
            break
        if preferred and prior["root_key"] in _confirmed_root_keys(
            session.evidence, subject
        ):
            executed = {str(row.get("command") or "") for row in session.probe_rounds}
            skipped = [command for command in ordered_extra if command not in executed]
            early_stopped = bool(skipped)
            break

    # Some family-specific observations do not represent generic root causes
    # (for example cloud-init state). They remain part of that family's compact
    # evidence contract and are collected after the competing generic causes.
    if not uses_triage and not early_stopped:
        active_commands = {
            _ACTIVE_ROOTS[root_id].probe
            for root_id in _FAMILY_ACTIVE_ROOTS.get(family or "", ())
        }
        for command in ordered_extra:
            if command not in BASELINE_PROBES and command not in active_commands:
                session.collect(command)
    elif not skipped:
        executed = {str(row.get("command") or "") for row in session.probe_rounds}
        skipped = [command for command in ordered_extra if command not in executed]

    # Direct subject probes are outside the generic sweep. They still run after a
    # triage early stop because a memory about a family cannot waive verification
    # of the concrete host/address the operator explicitly named.
    for command in subject_probes:
        if command not in BASELINE_PROBES and command not in ordered_extra:
            session.collect(command)

    original_probe_order = list(TRIAGE_PROBES if uses_triage else extra)
    reordered = ordered_extra != original_probe_order
    if prior["strictly_narrowed"] and (reordered or early_stopped):
        payload = {
            "session_id": session.session_id,
            "subject": subject or "local-system",
            "skills": list(prior["skills"]),
            "memory_ids": list(prior["memory_ids"]),
            "procedural_confidence": prior["procedural_confidence"],
            "preferred_probes": preferred,
            "candidate_probe_count": len(original_probe_order),
            "saved_probe_count": len(skipped) if early_stopped else 0,
            "skipped_probes": skipped if early_stopped else [],
            "original_probe_order": original_probe_order,
            "planned_probe_order": list(ordered_extra),
            "executed_probe_order": [
                command for command in ordered_extra if command not in skipped
            ],
            "effect": "probe_order_and_early_stop" if early_stopped else "probe_order",
            "confirmed_root_key": prior["root_key"] if early_stopped else None,
        }
        _persist_shortcut_trace(session, payload)

    if not terminal_case and not case_driven_policy:
        kinds: dict[str, int] = {}
        for item in session.retrieval_results:
            kind = str(item.get("kind") or "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
        # Persist retrieval only when it can affect the decision. A deterministic
        # local-in deny needs no empty memory receipt or zero-result case event.
        trace_results = [
            {
                key: value
                for key, value in item.items()
                if key not in {"summary", "source_trace_ids"}
            }
            for item in session.retrieval_results
        ]
        _persist_session_trace(session, "memory_candidates_ranked", {
            "scope": "interactive_investigation",
            "investigation_id": session.session_id,
            "question": session.question,
            "subject": session.subject,
            "family": session.family,
            "query_terms": query_terms,
            "returned_count": len(session.retrieval_results),
            "counts_by_kind": kinds,
            "results": trace_results,
        })
        _append_case_event(
            session,
            "retrieval_completed",
            {
                "sessionId": session.session_id,
                "returnedCount": len(session.retrieval_results),
                "countsByKind": kinds,
                "evidenceCount": len(session.evidence),
            },
            event_id=f"{session.session_id}:retrieval",
            status="investigating",
        )
    _persist_session(session)

    return _session_open_response(session)


def investigation_metrics(
    session: Session,
    *,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    """Derive comparable outcomes from the trace produced by real probe execution."""
    view = _hypothesis_view(session)
    confirmed = sorted(view.get("confirmed_root_keys") or ())
    evidence_to_round = {
        str(item.get("evidence_id")): index
        for index, item in enumerate(session.probe_rounds, start=1)
        if item.get("evidence_id")
    }
    confirmation_steps = [
        evidence_to_round[str(event.get("payload", {}).get("evidence_id"))]
        for event in session.trace_events
        if event.get("kind") == "hypothesis_confirmed"
        and str(event.get("payload", {}).get("evidence_id")) in evidence_to_round
    ]
    selected_context = [
        item for item in session.retrieval_results if item.get("selected_for_context")
    ]
    unscoped_context = [
        item for item in selected_context
        if item.get("kind") != "knowledge_document"
        and not item.get("matched_on")
        and not item.get("asset_ids")
    ]
    confirmed_support_ids = {
        str(evidence_id)
        for hypothesis in view.get("hypotheses") or ()
        if hypothesis.get("status") == "confirmed"
        for evidence_id in hypothesis.get("supporting_evidence_ids") or ()
    }
    decisive_outputs = {
        str(item.get("command") or ""): hashlib.sha256(
            json.dumps(
                {
                    "ok": bool(item.get("ok")),
                    "output": str(item.get("output") or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for item in session.evidence
        if str(item.get("evidence_id") or "") in confirmed_support_ids
        and str(item.get("command") or "")
    }
    return {
        "session_id": session.session_id,
        "memory_enabled": session.memory_enabled,
        "strategy": session.evaluation_strategy or (
            "full_system" if session.memory_enabled else "no_memory"
        ),
        "confirmed_roots": confirmed,
        "steps_to_first_confirmation": min(confirmation_steps) if confirmation_steps else None,
        "probe_count": len(session.probe_rounds),
        "saved_probe_count": max(
            0,
            len(set(session.probe_candidates) - set(BASELINE_PROBES))
            - len(session.probe_rounds),
        ),
        "probe_order": [str(item.get("command") or "") for item in session.probe_rounds],
        "candidate_probes": sorted(set(session.probe_candidates) - set(BASELINE_PROBES)),
        "selected_context_count": len(selected_context),
        "unscoped_context_count": len(unscoped_context),
        "memory_influenced_order": any(
            event.get("kind") == "memory_shortcut" for event in session.trace_events
        ),
        "retrieval_drop_count": sum(
            not bool(item.get("selected_for_context")) for item in session.retrieval_results
        ),
        "elapsed_ms": round(elapsed_ms, 3) if elapsed_ms is not None else None,
        "decisive_probe_output_fingerprints": decisive_outputs,
        "probe_output_fingerprints": {
            str(item.get("command") or ""): hashlib.sha256(
                json.dumps(
                    {
                        "ok": bool(item.get("ok")),
                        "output": str(item.get("output") or ""),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for item in session.evidence
            if str(item.get("command") or "") in {
                str(round_item.get("command") or "")
                for round_item in session.probe_rounds
            }
        },
    }


def _pair_log_path() -> Path:
    return Path(os.getenv(
        "AUTOPOIESIS_INVESTIGATION_PAIR_LOG",
        "/data/autopoiesis-production/investigation-pairs.jsonl",
    ))


def paired_evaluate_case(case_id: str) -> dict[str, Any]:
    """Execute fixed-script, no-memory and full-system arms on one fresh case."""
    from core.eval.investigation_pair import compare_investigation_pair
    from . import main
    from .investigation_cases import derive_investigation_scope
    import time
    from domains.network_rca.business_decision import is_terminal_local_in_deny

    case = main._case_repository().get(case_id)
    if case is None:
        raise ValueError("unknown investigation case")
    scope = derive_investigation_scope(case)
    if is_terminal_local_in_deny(dict(scope.get("incident_facts") or {})):
        raise ValueError("deterministic terminal case is not eligible for memory evaluation")
    if scope.get("scope_quality") == "unresolved":
        raise ValueError("case has no managed investigation scope")
    last_seen = datetime.fromisoformat(str(case.last_seen_at).replace("Z", "+00:00"))
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    observation_lag_seconds = (
        datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)
    ).total_seconds()
    max_lag = max(30.0, float(os.getenv(
        "AUTOPOIESIS_PAIR_MAX_OBSERVATION_LAG_SECONDS", "300"
    )))
    if observation_lag_seconds > max_lag:
        raise ValueError("case is outside the current-state evaluation window")
    repetitions = min(5, max(1, int(os.getenv("AUTOPOIESIS_PAIR_REPETITIONS", "1"))))
    samples: dict[str, list[tuple[Session, float]]] = {
        "fixed_script": [],
        "no_memory": [],
        "full_system": [],
    }
    for strategy, memory_enabled in (
        ("fixed_script", False),
        ("no_memory", False),
        ("full_system", True),
    ):
        for _sample_index in range(repetitions):
            began = time.perf_counter()
            opened = start(
                scope["question"],
                scope["family"],
                scope["subject"],
                case_id,
                memory_enabled=memory_enabled,
                evaluation_only=True,
                evaluation_strategy=strategy,
            )
            session = get(str(opened["session_id"]))
            loop = _loop_for(session)
            if strategy != "full_system" or _verified_memory_shortcut_root(session) is None:
                _advance_active_hypotheses(
                    session,
                    budget=len(loop.state.probes) if loop is not None else 0,
                )
            _persist_session(session)
            samples[strategy].append(
                (session, (time.perf_counter() - began) * 1000.0)
            )

    sample_metrics = {
        strategy: [
            investigation_metrics(session, elapsed_ms=elapsed_ms)
            for session, elapsed_ms in values
        ]
        for strategy, values in samples.items()
    }

    def representative(strategy: str) -> dict[str, Any]:
        ordered = sorted(
            sample_metrics[strategy], key=lambda item: float(item.get("elapsed_ms") or 0.0)
        )
        return dict(ordered[len(ordered) // 2])

    fixed = representative("fixed_script")
    control = representative("no_memory")
    treatment = representative("full_system")
    stable_roots = {
        strategy: len({
            tuple(item.get("confirmed_roots") or ())
            for item in values
        }) == 1
        for strategy, values in sample_metrics.items()
    }
    acceptance = compare_investigation_pair(control, treatment)
    fixed_comparison = compare_investigation_pair(fixed, treatment)
    if not all(stable_roots.values()):
        for comparison in (acceptance, fixed_comparison):
            comparison["business_value_proven"] = False
            comparison["failure_reason"] = "confirmed root changed across repeated executions"
    report = {
        "evaluation_id": f"pair-{uuid.uuid4().hex[:16]}",
        "case_id": case_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "observation_lag_seconds": round(max(0.0, observation_lag_seconds), 3),
        "input_mode": "fresh_probe_pair",
        "repetitions_per_strategy": repetitions,
        "stable_roots_by_strategy": stable_roots,
        "strategy_samples": sample_metrics,
        "fixed_script": fixed,
        "control": control,
        "treatment": treatment,
        "acceptance": acceptance,
        "fixed_script_comparison": fixed_comparison,
    }
    path = _pair_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    main._case_repository().append_event(
        case_id,
        kind="investigation_pair_measured",
        payload={"report": report},
        event_id=f"{report['evaluation_id']}:pair-measured",
    )
    return report


def _summarise(session: Session) -> str:
    """Return the current decision state using live observations."""
    if session.decision:
        return str(session.decision.get("headline") or session.decision.get("summary") or "")
    failed = [item for item in session.evidence if not item.get("ok")]
    if failed:
        return f"{len(failed)} 个所需数据源读取失败，案件保留当前 {len(session.evidence)} 条观察并继续调查。"
    if session.case_id:
        return f"案件已有 {len(session.evidence)} 条当前观察，正在形成关闭、处置或升级决定。"
    view = _hypothesis_view(session)
    active_count = len(view.get("active_root_keys") or ())
    return f"已取得 {len(session.evidence)} 条当前观察，仍有 {active_count} 个候选原因等待区分。"


def get(session_id: str) -> Session:
    session = _SESSIONS.get(session_id)
    if session is None:
        snapshot = _session_store().load(session_id)
        if snapshot is None:
            raise KeyError(f"unknown session {session_id!r}")
        session = Session.from_dict(snapshot)
        _SESSIONS[session_id] = session
    return session


def live_retrieval_trace(query: str | None = None, limit: int = 12) -> dict[str, Any]:
    """Project actual investigation retrieval receipts into the retrieval-page contract."""
    snapshots = _session_store().recent(limit=max(limit * 2, 20))
    seen = {str(item.get("session_id")) for item in snapshots}
    snapshots = [
        *[session.as_dict() for session in reversed(list(_SESSIONS.values())) if session.session_id not in seen],
        *snapshots,
    ]
    needle = (query or "").strip().lower()
    cases: list[dict[str, Any]] = []
    for snapshot in snapshots:
        results = list(snapshot.get("retrieval_results") or ())
        if not results:
            continue
        haystack = " ".join(str(snapshot.get(key) or "") for key in ("question", "subject", "family")).lower()
        if needle and needle not in haystack:
            continue
        documents: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[dict[str, Any]]] = {
            "asset_profile": [], "procedural": [], "episodic": [], "semantic": [],
        }
        selected: list[dict[str, Any]] = []
        for result in results:
            item_id = str(result.get("item_id") or result.get("locator") or "")
            if not item_id:
                continue
            kind = str(result.get("kind") or "")
            raw_tier = str(result.get("tier") or "")
            tier = raw_tier if raw_tier in grouped else (
                "asset_profile" if kind == "network_feature"
                else "procedural" if kind in {"procedural_memory", "operations_knowledge"}
                else "episodic" if kind == "incident_dossier"
                else "semantic"
            )
            summary = str(result.get("summary") or item_id)
            source = str(result.get("source") or "unknown")
            matched = [str(value) for value in result.get("matched_terms") or ()]
            raw_route = result.get("route")
            routes = [str(value) for value in raw_route] if isinstance(raw_route, (list, tuple)) else [str(raw_route or "")]
            via = ["lexical"] if any(
                value in {"bm25", "scope_and_recency_rank", "asset_exact", "exact_asset"}
                for value in routes
            ) else []
            relation = result.get("relation_to_current")
            if isinstance(relation, Mapping) and relation.get("matched_on"):
                via.append("graph")
            hit = {
                "doc_id": item_id,
                "rank": len(grouped[tier]) + 1,
                "score": float(result.get("score") or 0.0),
                "title": summary[:120],
                "snippet": summary[:220],
                "text": summary,
                "matched": matched,
                "via": via or ["lexical"],
            }
            grouped[tier].append(hit)
            documents[item_id] = {
                "title": hit["title"],
                "text": summary,
                "tags": [kind, raw_tier, *routes],
                "source": f"{source} · {result.get('locator') or item_id}",
            }
            if result.get("selected_for_context"):
                selected.append({
                    "doc_id": item_id,
                    "title": hit["title"],
                    "tokens": len(tokenize(summary)),
                    "reason": "selected_for_context receipt",
                    "text": summary,
                })
        session_id = str(snapshot.get("session_id") or "")
        question_text = str(snapshot.get("question") or "")
        cases.append({
            "id": f"investigate:{session_id}",
            "flow": "memory_recall",
            "label": {"zh": question_text[:80], "en": question_text[:80]},
            "query": question_text,
            "intent": {
                "tier": "live_investigation",
                "zh": "当前调查的统一检索回执",
                "en": "Unified retrieval receipt from a live investigation",
            },
            "corpus": {"size": len(documents), "source": "investigation retrieval receipt"},
            "triggers": {
                "count": 1,
                "live": True,
                "source": f"investigation-session:{session_id}",
                "note": {
                    "zh": f"案件 {snapshot.get('case_id') or '临时调查'} 的真实检索调用",
                    "en": f"Actual retrieval call for case {snapshot.get('case_id') or 'ad-hoc investigation'}",
                },
            },
            "tiers": [
                {
                    "id": tier,
                    "kind": tier,
                    "label": {
                        "zh": {"asset_profile": "设备资料", "procedural": "处理办法", "episodic": "历史事件", "semantic": "归纳与知识"}[tier],
                        "en": {"asset_profile": "ASSET INFO", "procedural": "HOW-TO", "episodic": "PAST INCIDENTS", "semantic": "PATTERNS AND KNOWLEDGE"}[tier],
                    },
                    "hits": hits,
                }
                for tier, hits in grouped.items()
            ],
            "graph": {"hops": 0, "expanded": []},
            "context": {
                "budget_tokens": sum(item["tokens"] for item in selected),
                "total_tokens": sum(item["tokens"] for item in selected),
                "selected": selected,
            },
            "docs": documents,
        })
        if len(cases) >= limit:
            break
    return {"ok": True, "dataMode": "live_investigation_receipts", "cases": cases}


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
    "root_hypothesis_id": "reuse a candidate id from state, or leave empty for a new root",
    "citations": ["ev-001"],
    "need_commands": ["a read-only command that would settle what is still unclear"],
    "verification": [
        {
            "command": "one diagnostic read-only command; use two independent signal families for a new root",
            "operator": "contains|not_contains|regex|not_regex|equals|not_equals",
            "value": "the exact observation that supports the root",
            "case_sensitive": False,
        }
    ],
    "runbook": [
        {"n": 1, "risk": "readonly|auto|gated", "what": "plain sentence", "command": "shell command", "why": "why this step"}
    ],
}

# Open causes often need one pass to choose checks, one pass to refine an
# initially missing signal, and one pass to name the root from both observations.
# The third pass is a hard ceiling: it applies only after the bounded catalogue
# has been exhausted and prevents an unbounded paid-model/tool loop.
MAX_ANALYZE_ROUNDS = 3


def _advance_active_hypotheses(session: Session, *, budget: int) -> list[dict[str, Any]]:
    """Collect the next bounded set of probes from the persisted competition."""
    collected: list[dict[str, Any]] = []
    for _index in range(max(0, budget)):
        item = _run_next_discriminating_probe(session)
        if item is None:
            break
        collected.append(item)
    return collected


def _archive_confirmed_if_complete(session: Session) -> dict[str, Any]:
    """Persist a learned root only after the whole competition has settled."""
    view = _hypothesis_view(session)
    if view.get("active_root_keys"):
        return {"committed": False, "reason": "competing_hypotheses_unresolved"}
    confirmed = [
        item for item in view.get("hypotheses") or ()
        if item.get("status") == "confirmed"
    ]
    if len(confirmed) != 1:
        return {"committed": False, "reason": "unique_confirmed_root_required"}
    hypothesis = confirmed[0]
    if not bool(hypothesis.get("archive_eligible")):
        return {"committed": False, "reason": "confirmed_signal_is_not_a_root_condition"}
    evidence_ids = [str(value) for value in hypothesis.get("supporting_evidence_ids") or ()]
    try:
        archived = close(
            session.session_id,
            resolution="confirmed",
            root_cause=str(hypothesis["statement"]),
            confirmed_by=f"hypothesis-loop:{session.session_id}",
            evidence_ids=evidence_ids,
            operator_note="deterministic hypothesis competition completed",
        )
    except RuntimeError:
        return {"committed": False, "reason": "operational_memory_unavailable"}
    except (KeyError, ValueError) as error:
        return {
            "committed": False,
            "reason": f"archive_rejected:{type(error).__name__}",
        }
    return {
        "committed": True,
        "dossier_id": str(archived.get("dossier", {}).get("dossier_id") or ""),
        "root_key": str(hypothesis.get("hypothesis_id") or ""),
        "evidence_ids": evidence_ids,
    }


def _remember_confirmed_procedure(
    session: Session,
    hypothesis: Mapping[str, Any],
) -> str | None:
    """Index the exact successful probes from a machine-confirmed incident."""
    memory = _live_memory_store()
    if memory is None:
        return None
    evidence_ids = {str(value) for value in hypothesis.get("supporting_evidence_ids") or ()}
    commands = list(dict.fromkeys(
        str(item.get("command") or "")
        for item in session.evidence
        if item.get("evidence_id") in evidence_ids
        and str(item.get("command") or "") in _memory_eligible_probes()
    ))
    if not commands:
        return None
    root_key = str(hypothesis.get("hypothesis_id") or "")
    identity = "\0".join((root_key, session.family or "", *(session.asset_ids or [])))
    memory_id = f"proc-live-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    observed_at = datetime.now(timezone.utc)
    existing = memory.get(memory_id)
    tags = [
        f"root:{root_key}",
        *(f"probe:{command}" for command in commands),
        *( [session.family] if session.family else [] ),
    ]
    if existing is None:
        memory.add(MemoryRecord(
            memory_id=memory_id,
            tier="procedural",
            text=f"verified {root_key}; successful checks: {', '.join(commands)}",
            tags=tags,
            asset_ids=list(session.asset_ids or ([session.subject] if session.subject else [])),
            evidence_ids=sorted(evidence_ids),
            confidence=1.5,
            source_trace_ids=[session.session_id],
            first_observed_at=observed_at,
            last_observed_at=observed_at,
            valid_from=observed_at,
            event_type=f"procedure:{root_key}",
        ))
    elif session.session_id not in existing.source_trace_ids:
        existing.confidence = min(3.0, float(existing.confidence) + 0.2)
        existing.strength = 1.0
        existing.last_observed_at = observed_at
        for value in tags:
            if value not in existing.tags:
                existing.tags.append(value)
        for value in session.asset_ids:
            if value not in existing.asset_ids:
                existing.asset_ids.append(value)
        existing.source_trace_ids.append(session.session_id)
        memory.reindex(memory_id)
    _persist_session_trace(session, "verified_procedure_indexed", {
        "session_id": session.session_id,
        "memory_id": memory_id,
        "root_hypothesis_id": root_key,
        "commands": commands,
        "evidence_ids": sorted(evidence_ids),
    })
    return memory_id


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
        "7. For a root not already present in the candidate state, declare two "
        "safe read-only commands from different diagnostic signals and their exact "
        "boolean verification predicates. Generic clock/load reads cannot verify a "
        "root. These declarations are frozen before execution. Reuse the "
        "returned root_hypothesis_id on the next round.\n"
        "Adapter probes are closed read-only tools: adapter:fortigate_context, "
        "adapter:device_history, and adapter:live_flows.\n"
        "Allowed host checks must be one argv-only command. Useful exact forms "
        "include `systemctl show UNIT -p PROPERTY --value`, `systemctl status "
        "UNIT --no-pager`, `journalctl -u UNIT -n 40 --no-pager`, and `curl -s "
        "-m 5 PRIVATE_URL`. Do not combine short flags or use variables, pipes, "
        "redirection, command substitution, or command chaining.\n"
        "8. Do not report the following as faults; they are normal here:\n"
        + "".join(f"   - {line}\n" for line in KNOWN_NORMAL)
        + f"9. Answer in {'Chinese' if language == 'zh' else 'English'}.\n"
        + "Return JSON only."
    )


def _settled_published_root(
    session: Session,
    payload: Mapping[str, Any],
    registered_id: str | None,
) -> RootCauseHypothesis | None:
    """Return the one fully tested root nominated by the analysis response."""
    loop = _loop_for(session)
    if loop is None:
        return None
    active = [
        item for item in loop.state.hypotheses
        if item.status in {"proposed", "testing"}
    ]
    confirmed = [item for item in loop.state.hypotheses if item.status == "confirmed"]
    if active or len(confirmed) != 1:
        return None
    candidate = confirmed[0]
    requested_id = str(payload.get("root_hypothesis_id") or registered_id or "").strip()
    statement = str(payload.get("root_cause") or "").strip().casefold()
    if requested_id and requested_id == candidate.hypothesis_id:
        return candidate
    if statement and statement != "inconclusive" and statement == candidate.statement.casefold():
        return candidate
    return None


def _failed_unit(session: Session) -> str | None:
    output = _output_for(session.evidence, "systemctl --failed --no-legend") or ""
    units = re.findall(r"\b[\w@.-]+\.(?:service|target|socket|timer)\b", output)
    if session.subject and session.subject in units:
        return session.subject
    return units[0] if len(set(units)) == 1 else None


def _down_interface(session: Session) -> str | None:
    output = _output_for(session.evidence, "ip -br link show") or ""
    candidates: list[str] = []
    for line in output.splitlines():
        columns = line.split()
        if not columns:
            continue
        name = columns[0].split("@")[0]
        if (
            re.fullmatch(r"(?:eth|eno|enp|ens)\w+", name)
            and name not in _EXPECTED_DOWN_INTERFACES
            and ("NO-CARRIER" in line.upper() or re.search(r"\bDOWN\b", line.upper()))
        ):
            candidates.append(name)
    if session.subject and session.subject in candidates:
        return session.subject
    return candidates[0] if len(set(candidates)) == 1 else None


def _verified_memory_shortcut_root(session: Session) -> RootCauseHypothesis | None:
    """Return a freshly confirmed root reached through an admitted recurrence.

    The shortcut is deliberately narrow: an exact managed scope, a verified
    procedural record, a strict subset of the normal probe catalogue, and a
    fresh decisive observation for the remembered root are all required.  A
    wrong memory therefore costs ordering only.  Its first probe is opposed and
    the ordinary competition continues.
    """
    if session.scope_quality != "exact" or not session.probe_prior.get("strictly_narrowed"):
        return None
    if float(session.probe_prior.get("procedural_confidence") or 0.0) < 1.5:
        return None
    shortcut = next(
        (
            dict(event.get("payload") or {})
            for event in reversed(session.trace_events)
            if event.get("kind") == "memory_shortcut"
        ),
        None,
    )
    if not shortcut or shortcut.get("effect") != "probe_order_and_early_stop":
        return None
    if int(shortcut.get("saved_probe_count") or 0) <= 0:
        return None
    root_id = str(shortcut.get("confirmed_root_key") or "")
    if not root_id or root_id != str(session.probe_prior.get("root_key") or ""):
        return None
    loop = _loop_for(session)
    if loop is None:
        return None
    confirmed = [item for item in loop.state.hypotheses if item.status == "confirmed"]
    if len(confirmed) != 1 or confirmed[0].hypothesis_id != root_id:
        return None
    if not confirmed[0].supporting_evidence_ids or confirmed[0].opposing_evidence_ids:
        return None
    return confirmed[0]


def action_candidate(session_id: str) -> dict[str, Any]:
    """Map one settled root to a preflighted, allowlisted recovery action."""
    session = get(session_id)
    loop = _loop_for(session)
    if loop is None:
        return {"eligible": False, "reason": "hypothesis_state_missing"}
    active = [item for item in loop.state.hypotheses if item.status in {"proposed", "testing"}]
    confirmed = [item for item in loop.state.hypotheses if item.status == "confirmed"]
    shortcut_root = _verified_memory_shortcut_root(session)
    if active and shortcut_root is not None:
        confirmed = [shortcut_root]
        active = []
    if active or len(confirmed) != 1:
        return {"eligible": False, "reason": "unique_settled_root_required"}
    root = confirmed[0]
    mapping: tuple[str, str | None] | None = None
    if root.hypothesis_id == "service_failed":
        mapping = ("restart_unit", _failed_unit(session))
    elif root.hypothesis_id == "carrier_down":
        mapping = ("bounce_interface", _down_interface(session))
    if mapping is None:
        return {
            "eligible": False,
            "reason": "no_allowlisted_action_for_root",
            "root_hypothesis_id": root.hypothesis_id,
        }
    action, target = mapping
    if target is None:
        return {
            "eligible": False,
            "reason": "single_action_target_required",
            "root_hypothesis_id": root.hypothesis_id,
            "action": action,
        }
    from .remediation import preflight

    check = preflight(action, target)
    policy = dict(check.get("policy") or {})
    auto_execute_allowed = bool(
        check.get("eligible")
        and session.case_id
        and session.auto_started
        and session.scope_quality == "exact"
        and target == session.subject
        and policy.get("auto_execute") is True
    )
    return {
        **check,
        "root_hypothesis_id": root.hypothesis_id,
        "root_statement": root.statement,
        "supporting_evidence_ids": list(root.supporting_evidence_ids),
        "auto_execute_allowed": auto_execute_allowed,
    }


def _record_business_decision(session: Session, decision: Any) -> dict[str, Any]:
    """Persist the one operator-facing result for this investigation state."""
    payload = decision.as_dict()
    session.decision = payload
    state = str(payload.get("state") or "investigating")
    case_status = {
        "resolved": "resolved",
        "escalated": "escalated",
        "action_ready": "waiting",
        "observing": "waiting",
    }.get(state, "investigating")
    identity = json.dumps(
        {
            "state": state,
            "classification": payload.get("classification"),
            "action": payload.get("action"),
            "readback": payload.get("readback"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    _append_case_event(
        session,
        "business_decision_recorded",
        {"sessionId": session.session_id, "decision": payload},
        event_id=f"{session.session_id}:decision:{event_digest}",
        status=case_status,
    )
    _persist_session_trace(session, "business_decision_recorded", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "state": state,
        "classification": payload.get("classification"),
        "evidence_ids": [
            item.get("evidenceId") for item in payload.get("evidence") or ()
        ],
        "next_probe": payload.get("nextProbe"),
        "case_status": case_status,
    })
    _persist_session(session)
    return {"decision": payload, "case_status": case_status}


def _evidence_for_root(
    session: Session,
    evidence_ids: list[str] | tuple[str, ...],
) -> tuple[Any, ...]:
    from domains.network_rca.business_decision import DecisionEvidence

    selected = set(str(value) for value in evidence_ids)
    return tuple(
        DecisionEvidence(
            evidence_id=str(item.get("evidence_id") or ""),
            label=(
                f"{dict(item.get('claim_support') or {}).get('signalFamily')} 根因支持检查"
                if item.get("claim_support") else str(item.get("command") or "observation")
            ),
            value=(
                f"{dict(item.get('claim_support') or {}).get('operator')} "
                f"{dict(item.get('claim_support') or {}).get('expected')!r}，"
                f"匹配={dict(item.get('claim_support') or {}).get('matched')}；"
                f"原始结果：{_excerpt(str(item.get('output') or ''), 140, 60)}"
                if item.get("claim_support")
                else _excerpt(str(item.get("output") or ""), 180, 80)
            ),
            source=str(item.get("source") or "live_tool"),
            observed_at=str(item.get("at") or "") or None,
        )
        for item in session.evidence
        if item.get("evidence_id") in selected
    )


def _confirmed_root_business_text(
    session: Session,
    root: Mapping[str, Any],
    *,
    memory_shortcut: bool,
    action_eligible: bool,
) -> tuple[str, str, str]:
    """Translate a confirmed mechanism into the concrete operator decision."""
    root_id = str(root.get("hypothesis_id") or "confirmed_root")
    subject = session.subject or "受管对象"
    facts = dict(session.incident_facts or {})
    if root_id == "duplicate_ip_static":
        measured = dict(facts.get("measured") or {})
        verification = dict(facts.get("verification") or {})
        macs = [str(value) for value in measured.get("macs") or ()]
        handovers = int(measured.get("handovers") or 0)
        summary = str(verification.get("note_zh") or verification.get("note") or "").strip()
        if not summary:
            summary = (
                f"{subject} 在 {len(macs)} 个 MAC 间发生 {handovers} 次归属切换，"
                "当前 L2 采集与 DHCP 绑定不一致。"
            )
        return (
            f"已确认 {subject} 存在地址归属冲突",
            summary,
            (
                "按记录中的冲突 MAC 查询交换机端口，确认合法设备后调整静态地址或 DHCP 保留；"
                "变更完成后重新采集 ARP、DHCP 与连续 L2 归属。"
            ),
        )
    if root_id == "admin_bruteforce_lockout":
        output = _output_for(session.evidence, "adapter:admin_auth_window") or "{}"
        try:
            measured = dict(json.loads(output))
        except (TypeError, ValueError):
            measured = {}
        failures = int(measured.get("failed_logins") or facts.get("failedLogins") or 0)
        distinct = int(measured.get("distinct_sources") or facts.get("distinctSources") or 0)
        lockouts = int(measured.get("lockouts") or facts.get("lockouts") or 0)
        vpn_entry = str(facts.get("trafficSubtype") or "").casefold() == "vpn"
        entry_name = "SSL VPN 认证入口" if vpn_entry else "防火墙管理入口"
        follow_up = (
            "由设备负责人核对被尝试账户和合法来源，启用多因素认证并限制 SSL VPN 来源范围；"
            "策略提交后重新查询失败认证、账户锁定与成功登录记录。"
            if vpn_entry else
            "由设备负责人确认合法管理来源，收紧管理账户 trusthost 或关闭公网管理入口；"
            "策略提交后重新查询失败登录、锁定与管理口放行记录。"
        )
        return (
            f"已确认{entry_name}遭遇分布式登录攻击",
            f"事件窗口内记录 {failures} 次失败登录，来自 {distinct} 个来源地址，触发 {lockouts} 次锁定。",
            follow_up,
        )
    labels = {
        "carrier_down": "受管物理网口失去载波",
        "default_route_missing": "主机缺少可用默认路由",
        "neighbor_unreachable": "目标地址当前无法完成邻居解析",
        "service_failed": "受管服务进入失败状态",
        "disk_pressure": "可写文件系统达到容量阈值",
        "memory_pressure": "主机可用内存低于阈值",
        "healthcheck_failed": "调查服务健康检查失败",
    }
    headline = f"已确认：{labels.get(root_id, str(root.get('statement') or root_id))}"
    summary = (
        "复发记忆把已验证探针排到首位，当前读数再次确认同一根因。"
        if memory_shortcut
        else "当前只读观察完成候选原因排查，并确认该故障条件。"
    )
    disposition = (
        "允许执行已通过前置检查的单一动作，动作完成后回读原系统。"
        if action_eligible
        else "当前动作目录没有适用的安全操作，案件携带已确认根因转交责任人。"
    )
    return headline, summary, disposition


def complete(session_id: str) -> dict[str, Any]:
    """Advance a session until it has a business decision or an exact next probe."""
    from domains.network_rca.business_decision import (
        BusinessDecision,
        DecisionEvidence,
        MissingObservation,
        is_terminal_local_in_deny,
        local_in_deny_decision,
        pending_policy_decision,
    )

    session = get(session_id)
    case_evidence = next(
        (
            item for item in session.evidence
            if str(item.get("command") or "").startswith("case:")
        ),
        session.evidence[0] if session.evidence else None,
    )
    case_evidence_id = str((case_evidence or {}).get("evidence_id") or "")
    if session.case_id and session.scope_quality == "unresolved":
        decision = BusinessDecision(
            case_id=session.case_id,
            session_id=session.session_id,
            state="investigating",
            classification="incident_scope_unresolved",
            headline="事件已接管，当前记录无法圈定可调查的受管对象",
            summary="源记录缺少能够把告警绑定到受管资产和故障域的结构化字段。",
            disposition="保持案件调查中，从源告警补齐资产身份后再启动探针。",
            action="当前不执行探针或变更。",
            impacted_assets=tuple(session.asset_ids),
            missing_observations=tuple(
                MissingObservation(
                    code=f"scope:{name}",
                    question=f"事件的 {name} 是什么？",
                )
                for name in (session.scope_missing or ["managedAsset"])
            ),
        )
        return {
            **_record_business_decision(session, decision),
            "evidence_total": len(session.evidence),
            "hypothesis_state": _hypothesis_view(session),
            "probe_rounds": session.probe_rounds,
            "action_candidate": {"eligible": False, "reason": "incident_scope_unresolved"},
        }
    if session.case_id and session.family == "fam-policy-reachability":
        if is_terminal_local_in_deny(session.incident_facts):
            session.historical_context = {}
            session.knowledge_context = []
            session.retrieval_results = []
            session.probe_prior = {}
            session.trace_events = [
                event for event in session.trace_events
                if event.get("kind") not in {
                    "operational_memory_recalled",
                    "knowledge_retrieved",
                    "memory_candidates_ranked",
                    "retrieval_candidates_filtered",
                }
            ]
            decision = local_in_deny_decision(
                case_id=session.case_id,
                session_id=session.session_id,
                facts=session.incident_facts,
                evidence_id=case_evidence_id,
                managed_gateway=session.subject or "192.168.1.1",
            )
        else:
            completed = [
                str(item.get("command") or "")
                for item in session.evidence
                if item.get("ok")
                and str(item.get("command") or "").startswith("adapter:")
            ]
            decision = pending_policy_decision(
                case_id=session.case_id,
                session_id=session.session_id,
                facts=session.incident_facts,
                evidence_id=case_evidence_id,
                managed_gateway=session.subject or "192.168.1.1",
                completed_probes=completed,
            )
        return {
            **_record_business_decision(session, decision),
            "evidence_total": len(session.evidence),
            "hypothesis_state": _hypothesis_view(session),
            "probe_rounds": session.probe_rounds,
            "action_candidate": action_candidate(session.session_id),
        }

    loop = _loop_for(session)
    shortcut_root = _verified_memory_shortcut_root(session)
    if loop is not None and shortcut_root is None:
        _advance_active_hypotheses(session, budget=len(loop.state.probes))
    view = _hypothesis_view(session)
    active = list(view.get("active_root_keys") or ())
    confirmed = [
        item for item in view.get("hypotheses") or ()
        if item.get("status") == "confirmed"
    ]
    if shortcut_root is not None:
        active = []
        confirmed = [shortcut_root.model_dump(mode="json")]
    case_key = session.case_id or f"adhoc:{session.session_id}"
    procedure_memory_id: str | None = None
    if active:
        available = next(
            (
                item for item in view.get("probes") or ()
                if item.get("status") == "available"
            ),
            None,
        )
        next_probe = str((available or {}).get("description") or "") or None
        decision = BusinessDecision(
            case_id=case_key,
            session_id=session.session_id,
            state="investigating",
            classification="root_cause_unresolved",
            headline="调查仍缺少能够区分候选原因的当前观察",
            summary=f"还有 {len(active)} 个候选原因未完成检查，案件保持调查中。",
            disposition="继续执行下一项只读探针；探针失败时保留案件并报告数据源故障。",
            action="当前不执行变更。",
            impacted_assets=tuple(session.asset_ids),
            missing_observations=tuple(
                MissingObservation(
                    code=f"hypothesis:{root_id}",
                    question=f"候选原因 {root_id} 在当前对象上是否成立？",
                    probe=next_probe,
                )
                for root_id in active[:4]
            ),
            next_probe=next_probe,
        )
    elif len(confirmed) == 1:
        root = confirmed[0]
        supports = list(root.get("supporting_evidence_ids") or ())
        if session.case_id and bool(root.get("archive_eligible")):
            procedure_memory_id = _remember_confirmed_procedure(session, root)
        candidate = action_candidate(session.session_id)
        eligible = bool(candidate.get("eligible"))
        headline, summary, disposition = _confirmed_root_business_text(
            session,
            root,
            memory_shortcut=shortcut_root is not None,
            action_eligible=eligible,
        )
        decision = BusinessDecision(
            case_id=case_key,
            session_id=session.session_id,
            state="action_ready" if eligible else "escalated",
            classification=str(root.get("hypothesis_id") or "confirmed_root"),
            headline=headline,
            summary=summary,
            disposition=disposition,
            action=(
                f"{candidate.get('action')} {candidate.get('target')}"
                if eligible else "不执行自动动作"
            ),
            impacted_assets=tuple(session.asset_ids),
            evidence=_evidence_for_root(session, supports),
        )
    elif len(confirmed) > 1:
        ids = [str(item.get("hypothesis_id") or "") for item in confirmed]
        supporting_ids = list(dict.fromkeys(
            str(evidence_id)
            for item in confirmed
            for evidence_id in item.get("supporting_evidence_ids") or ()
        ))
        decision = BusinessDecision(
            case_id=case_key,
            session_id=session.session_id,
            state="investigating",
            classification="multiple_confirmed_conditions",
            headline="多个故障条件同时成立",
            summary="当前观察确认了多个独立故障条件，需要按影响面分别处置。",
            disposition="拆分动作并逐项回读，任何一项失败都保留原案件。",
            action="等待按故障条件生成独立动作",
            impacted_assets=tuple(session.asset_ids),
            evidence=_evidence_for_root(session, supporting_ids),
        )
    else:
        decision = BusinessDecision(
            case_id=case_key,
            session_id=session.session_id,
            state="investigating",
            classification="open_root_required",
            headline="预定义故障条件已排除，转入开放根因调查",
            summary="当前只读结果排除了已定义的链路、路由、服务、磁盘和内存条件，告警原因仍未解释。",
            disposition="由调查模型提出可反证的新根因，并自动执行两个独立信号的只读检查。",
            action="根因确认前不执行变更。",
            impacted_assets=tuple(session.asset_ids),
            missing_observations=(MissingObservation(
                code="open_root_hypothesis_required",
                question="哪一个新的可反证故障条件能够同时解释当前症状和两个独立观察？",
                probe="agent:propose-open-root",
            ),),
            next_probe="agent:propose-open-root",
        )
    return {
        **_record_business_decision(session, decision),
        "evidence_total": len(session.evidence),
        "hypothesis_state": _hypothesis_view(session),
        "probe_rounds": session.probe_rounds,
        "action_candidate": action_candidate(session.session_id),
        "procedure_memory_id": procedure_memory_id,
    }


def remediate(
    session_id: str,
    *,
    executor: Any | None = None,
) -> dict[str, Any]:
    """Execute the investigation's eligible action and ingest its readback."""
    session = get(session_id)
    candidate = action_candidate(session_id)
    if not candidate.get("eligible"):
        return {"ran": False, "refused": True, **candidate}
    if executor is None:
        from .remediation import execute as executor
    action = str(candidate["action"])
    target = str(candidate["target"])
    root_id = str(candidate["root_hypothesis_id"])
    _append_case_event(
        session,
        "remediation_started",
        {
            "sessionId": session.session_id,
            "rootHypothesisId": root_id,
            "action": action,
            "target": target,
        },
        event_id=f"{session.session_id}:remediation-started:{root_id}:{action}:{target}",
        status="waiting",
    )
    _persist_session_trace(session, "remediation_started", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "root_hypothesis_id": root_id,
        "action": action,
        "target": target,
        "fault_domain": session.fault_domain,
    })
    try:
        result = executor(
            action,
            target,
            incident_id=session.case_id or session.session_id,
            failure_domain=session.fault_domain or session.family or root_id,
            idempotency_key=f"investigate:{session.session_id}:{root_id}:{action}:{target}",
        )
    except Exception as error:  # action failures become a terminal case outcome
        result = {
            "ran": False,
            "action": action,
            "target": target,
            "outcome": "failed",
            "needs_human": True,
            "execution_id": f"failed-{session.session_id}-{root_id}",
            "at": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(error).__name__}: {error}"[:300],
        }
    receipt = session.collect_observation(
        label=f"action_readback:{action}:{target}",
        payload=result,
        ok=bool(result.get("ran")) and result.get("outcome") == "passed",
        observed_at=str(result.get("at") or datetime.now(timezone.utc).isoformat()),
        source="action_readback",
    )
    needs_human = bool(result.get("needs_human"))
    passed = bool(result.get("ran")) and result.get("outcome") == "passed"
    case_status = "resolved" if passed else ("escalated" if needs_human else "investigating")
    _append_case_event(
        session,
        "remediation_completed",
        {
            "sessionId": session.session_id,
            "rootHypothesisId": root_id,
            "action": action,
            "target": target,
            "executionId": result.get("execution_id"),
            "outcome": result.get("outcome"),
            "readbackEvidenceId": receipt.get("evidence_id"),
            "needsHuman": needs_human,
        },
        event_id=f"{session.session_id}:remediation:{result.get('execution_id') or action + ':' + target}",
        status=case_status,
    )
    _persist_session_trace(session, "action_readback_recorded", {
        "session_id": session.session_id,
        "case_id": session.case_id,
        "root_hypothesis_id": root_id,
        "action": action,
        "target": target,
        "outcome": result.get("outcome"),
        "evidence_id": receipt.get("evidence_id"),
        "case_status": case_status,
    })
    from domains.network_rca.business_decision import BusinessDecision, DecisionEvidence

    previous = dict(session.decision or {})
    previous_evidence = tuple(
        DecisionEvidence(
            evidence_id=str(item.get("evidenceId") or ""),
            label=str(item.get("label") or "observation"),
            value=str(item.get("value") or ""),
            source=str(item.get("source") or "live_tool"),
            observed_at=item.get("observedAt"),
        )
        for item in previous.get("evidence") or ()
        if item.get("evidenceId")
    )
    readback_evidence = DecisionEvidence(
        evidence_id=str(receipt.get("evidence_id") or ""),
        label="动作结果回读",
        value=f"{action} {target} -> {result.get('outcome') or 'unknown'}",
        source="action_readback",
        observed_at=str(receipt.get("at") or "") or None,
    )
    final_decision = BusinessDecision(
        case_id=session.case_id or f"adhoc:{session.session_id}",
        session_id=session.session_id,
        state="resolved" if passed else "escalated",
        classification=str(previous.get("classification") or root_id),
        headline=(
            "动作完成且结果回读通过"
            if passed else "动作未通过结果回读，案件已升级"
        ),
        summary=(
            f"{action} {target} 已执行，观察结果为 {result.get('outcome') or 'unknown'}。"
        ),
        disposition=(
            "关闭案件；同一故障复发时按复发预算重新开案。"
            if passed else "停止继续变更，保留现场并调查动作失败原因。"
        ),
        action=f"{action} {target}",
        impacted_assets=tuple(session.asset_ids or ([target] if target else [])),
        evidence=(*previous_evidence, readback_evidence),
        readback={
            "outcome": result.get("outcome"),
            "needsHuman": needs_human,
            "executionId": result.get("execution_id"),
            "evidenceId": receipt.get("evidence_id"),
        },
    )
    business = _record_business_decision(session, final_decision)
    _persist_session(session)
    return {
        **result,
        "candidate": candidate,
        "readback_evidence": receipt,
        "case_status": business["case_status"],
        "decision": business["decision"],
    }


def analyze(
    session_id: str,
    language: str = "zh",
    *,
    client_override: Any | None = None,
) -> dict[str, Any]:
    """Turn collected evidence into a diagnosis and a graded runbook."""
    session = get(session_id)
    if session.case_id:
        completed = complete(session_id)
        decision = dict(completed.get("decision") or {})
        if decision.get("classification") != "open_root_required":
            return {
                **completed,
                "diagnosis": str(decision.get("summary") or ""),
                "root_cause": str(decision.get("classification") or ""),
                "citations": [
                    str(item.get("evidenceId") or "")
                    for item in decision.get("evidence") or ()
                    if item.get("evidenceId")
                ],
                "runbook": [],
                "follow_up_evidence": [],
                "action_candidate": action_candidate(session_id),
                "degraded": False,
            }
    client = client_override if client_override is not None else _client()
    if client is None:
        # Deterministic evidence collection remains useful when a paid model is
        # unavailable.  Finish the bounded candidate set and expose its state.
        collected = _advance_active_hypotheses(session, budget=2)
        memory_commit = (
            {"committed": False, "reason": "case_decision_owns_disposition"}
            if session.case_id else _archive_confirmed_if_complete(session)
        )
        _persist_session(session)
        result = {
            "diagnosis": "未配置推理模型，只给出已采集的证据。设置 AUTOPOIESIS_LLM_BASE_URL / "
                         "AUTOPOIESIS_LLM_MODEL / AUTOPOIESIS_LLM_API_KEY 后可生成处置方案。",
            "citations": [],
            "runbook": [],
            "follow_up_evidence": collected,
            "hypothesis_state": _hypothesis_view(session),
            "probe_rounds": session.probe_rounds,
            "memory_commit": memory_commit,
            "action_candidate": action_candidate(session.session_id),
            "degraded": True,
        }
        if session.case_id:
            result["decision"] = dict(session.decision)
            result["case_status"] = "investigating"
        return result

    payload: dict[str, Any] = {}
    collected: list[dict[str, Any]] = []
    registered_id: str | None = None

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
                    "以下检索结果来自历史事件、记忆索引和参考知识，用于提出与排序假设。"
                    "每条结果都携带来源、分数和与本次调查的匹配关系。"
                    "现场状态和根因结论仍须引用本次会话的新鲜证据。\n"
                    f"检索结果：{json.dumps(_retrieval_context(session), ensure_ascii=False)}\n\n"
                    "以下候选根因状态由当前会话的探针结果维护。confirmed 和 rejected "
                    "可作为当前事实，proposed 和 testing 仍是待验证候选。\n"
                    f"候选状态：{json.dumps(_hypothesis_view(session), ensure_ascii=False)}\n\n"
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

        registered_id = _register_model_hypothesis(session, payload) or registered_id
        raw_verification_commands = [
            str(item.get("command") or "").strip()
            for item in (payload.get("verification") or ())
            if isinstance(item, Mapping) and str(item.get("command") or "").strip()
        ]
        # Execute a proposed verification command only when the hypothesis
        # aggregate accepted it as one of the frozen, diagnostically relevant
        # checks. Safe-but-generic reads remain available as ordinary probes,
        # but a model cannot make them part of an open-root proof by naming them
        # in JSON.
        verification_commands: list[str] = []
        registered_loop = _loop_for(session)
        if registered_id and registered_loop is not None:
            accepted_checks = {
                item.description
                for item in registered_loop.state.probes
                if registered_id in item.distinguishes_hypothesis_ids
                and item.observation_predicate is not None
            }
            verification_commands = [
                command for command in raw_verification_commands
                if command in accepted_checks
            ]
        wanted = list(dict.fromkeys([
            *verification_commands,
            *[
                str(command).strip()
                for command in (payload.get("need_commands") or ())
                if str(command).strip()
            ],
        ]))
        if round_index == MAX_ANALYZE_ROUNDS - 1:
            break
        if wanted:
            for command in wanted[:8]:
                collected.append(session.collect(command))
        else:
            # A model that returns inconclusive without asking for a useful
            # observation does not end the investigation.  The persisted loop
            # supplies one next probe and the second pass sees its fresh output.
            advanced = _advance_active_hypotheses(session, budget=1)
            collected.extend(advanced)
            if not advanced:
                break

    # Settle every bounded catalogue candidate before publishing a root.  This
    # is local read-only work; leaving competitors untested creates a cheap but
    # misleading answer and makes the paired evaluation meaningless.
    loop = _loop_for(session)
    remaining = len(loop.state.probes) if loop is not None else 0
    collected.extend(_advance_active_hypotheses(session, budget=remaining))
    published = _settled_published_root(session, payload, registered_id)
    runbook = _sanitise_runbook(payload.get("runbook") or [])
    session.runbook = runbook
    if published is None:
        session.diagnosis = "现有观察仍有多个可能原因，调查继续采集能够区分它们的只读结果。"
        root_cause = "inconclusive"
        citations = []
    else:
        session.diagnosis = str(payload.get("diagnosis") or published.statement)
        root_cause = published.statement
        support_ids = set(published.supporting_evidence_ids)
        claimed = _verified_citations(session, payload.get("citations") or [])
        citations = [item for item in claimed if item in support_ids]
        if not citations:
            citations = list(published.supporting_evidence_ids)
    session.root_cause = root_cause
    session.analysis_citations = citations
    memory_commit = (
        {"committed": False, "reason": "case_decision_owns_disposition"}
        if session.case_id else _archive_confirmed_if_complete(session)
    )
    _append_case_event(
        session,
        "hypothesis_updated",
        {
            "sessionId": session.session_id,
            "rootCause": root_cause or "inconclusive",
            "citations": citations,
            "evidenceTotal": len(session.evidence),
        },
        event_id=f"{session.session_id}:analysis:{len(session.turns)}:{len(session.evidence)}",
        status="waiting" if published is not None else "investigating",
    )
    _persist_session(session)
    result = {
        "diagnosis": session.diagnosis,
        "root_cause": root_cause,
        "citations": citations,
        "runbook": runbook,
        # What the model asked for and the harness ran, so the reader can see
        # the investigation deepened rather than stalling on a request.
        "follow_up_evidence": collected,
        "evidence_total": len(session.evidence),
        "summary": _summarise(session),
        "hypothesis_state": _hypothesis_view(session),
        "probe_rounds": session.probe_rounds,
        "memory_commit": memory_commit,
        "action_candidate": action_candidate(session.session_id),
        "degraded": False,
    }
    if session.case_id:
        completed = complete(session.session_id)
        decision = dict(completed.get("decision") or {})
        result.update(completed)
        result["diagnosis"] = str(decision.get("summary") or session.diagnosis)
        result["root_cause"] = str(decision.get("classification") or root_cause)
        result["citations"] = [
            str(item.get("evidenceId") or "")
            for item in decision.get("evidence") or ()
            if item.get("evidenceId")
        ]
    return result


def close(
    session_id: str,
    *,
    resolution: str,
    root_cause: str,
    confirmed_by: str,
    evidence_ids: list[str] | tuple[str, ...] = (),
    operator_note: str | None = None,
) -> dict[str, Any]:
    """Archive an operator disposition or a fully settled mechanical result."""
    from domains.network_rca.incident_dossier import (
        EvidenceReference,
        IncidentDossier,
        RootCauseHypothesis,
    )
    from . import main

    session = get(session_id)
    resolution = resolution.strip().lower()
    if resolution not in {"confirmed", "inconclusive", "refuted"}:
        raise ValueError("unsupported investigation resolution")
    root_cause = root_cause.strip()
    confirmed_by = confirmed_by.strip()
    if not root_cause or not confirmed_by:
        raise ValueError("root_cause and confirmed_by are required")
    machine_confirmation = confirmed_by == f"hypothesis-loop:{session.session_id}"
    confirmed_hypothesis: dict[str, Any] | None = None
    if machine_confirmation:
        view = _hypothesis_view(session)
        active = list(view.get("active_root_keys") or ())
        matching = [
            item for item in view.get("hypotheses") or ()
            if item.get("status") == "confirmed" and item.get("statement") == root_cause
        ]
        if resolution != "confirmed" or active or len(matching) != 1:
            raise ValueError(
                "machine confirmation requires one settled confirmed hypothesis"
            )
        confirmed_hypothesis = matching[0]
    service = getattr(main, "_operational_memory", None)
    if service is None:
        raise RuntimeError("operational memory is unavailable")
    dossier_id = f"investigate:{session.session_id}"
    existing = service.dossiers.get(dossier_id)
    if existing is not None:
        prior = existing.root_causes[0] if existing.root_causes else None
        expected_status = "hypothesis" if resolution == "inconclusive" else resolution
        if (
            prior is not None
            and prior.statement == root_cause
            and prior.status == expected_status
            and (prior.confirmed_by or confirmed_by) == confirmed_by
        ):
            return {"dossier": existing.model_dump(mode="json"), "resolution": resolution}
        raise ValueError("investigation already has a different archived disposition")

    machine_evidence = (
        list(confirmed_hypothesis.get("supporting_evidence_ids") or ())
        if confirmed_hypothesis is not None
        else []
    )
    selected = list(dict.fromkeys(evidence_ids or machine_evidence or session.analysis_citations))
    unknown = sorted(set(selected) - session.evidence_ids())
    if unknown:
        raise ValueError("unknown session evidence: " + ", ".join(unknown))
    if resolution in {"confirmed", "refuted"} and not selected:
        raise ValueError("confirmed or refuted root cause requires fresh session evidence")

    now = datetime.now(timezone.utc)
    references: list[EvidenceReference] = []
    dossier_id_by_session_id: dict[str, str] = {}
    for item in session.evidence:
        evidence_id = f"investigate:{session.session_id}:{item['evidence_id']}"
        dossier_id_by_session_id[item["evidence_id"]] = evidence_id
        output = str(item.get("output") or "")
        observed_at = datetime.fromisoformat(
            str(item.get("at") or session.opened_at).replace("Z", "+00:00")
        )
        references.append(EvidenceReference(
            evidence_id=evidence_id,
            source_type="telemetry",
            locator=f"investigate-session:{session.session_id}:{item['evidence_id']}",
            observed_at=observed_at,
            summary=f"{item.get('command')}: {'ok' if item.get('ok') else 'failed'}",
            content_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        ))
    root_status = "hypothesis" if resolution == "inconclusive" else resolution
    root_evidence_ids = [dossier_id_by_session_id[item] for item in selected]
    if not machine_confirmation:
        operator_payload = json.dumps(
            {
                "confirmed_by": confirmed_by,
                "note": operator_note or "",
                "resolution": resolution,
                "root_cause": root_cause,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        operator_evidence_id = f"investigate:{session.session_id}:operator-disposition"
        references.append(EvidenceReference(
            evidence_id=operator_evidence_id,
            source_type="operator",
            locator=f"investigate-session:{session.session_id}:operator-disposition",
            observed_at=now,
            summary=operator_note or f"operator marked root cause {resolution}",
            content_sha256=hashlib.sha256(operator_payload.encode("utf-8")).hexdigest(),
        ))
        root_evidence_ids.append(operator_evidence_id)
    root = RootCauseHypothesis(
        hypothesis_id=f"investigate-root:{session.session_id}",
        statement=root_cause,
        status=root_status,
        origin="analysis" if machine_confirmation else "operator",
        confidence=1.0 if resolution == "confirmed" else 0.0,
        evidence_ids=tuple(root_evidence_ids),
        updated_at=now,
        confirmed_by=confirmed_by if resolution == "confirmed" else None,
    )
    opened_at = datetime.fromisoformat(session.opened_at.replace("Z", "+00:00"))
    fingerprint = hashlib.sha256(
        f"{session.family or 'general'}\0{session.subject or 'local'}\0{session.question}".encode("utf-8")
    ).hexdigest()
    dossier = IncidentDossier(
        dossier_id=dossier_id,
        source_mode="live",
        status="escalated" if resolution == "inconclusive" else "investigating",
        fault_family=session.family or "general_investigation",
        fault_summary=session.question,
        severity="medium",
        symptom_fingerprint=fingerprint,
        asset_ids=(session.subject or "local-system",),
        opened_at=opened_at,
        updated_at=now,
        evidence=tuple(references),
        root_causes=(root,),
    )
    payload = service.save_dossier(dossier)
    procedure_memory_id = (
        _remember_confirmed_procedure(session, confirmed_hypothesis)
        if machine_confirmation and confirmed_hypothesis is not None
        else None
    )
    session.trace_events.append({
        "kind": "incident_dossier_saved",
        "at": now.isoformat(),
        "payload": {"dossier_id": dossier.dossier_id, "resolution": resolution},
    })
    final_status = "escalated" if resolution == "inconclusive" else "resolved"
    _append_case_event(
        session,
        "investigation_disposition_recorded",
        {
            "sessionId": session.session_id,
            "dossierId": dossier.dossier_id,
            "resolution": resolution,
            "rootCause": root_cause,
            "citations": selected,
            "confirmedBy": confirmed_by,
        },
        event_id=f"{session.session_id}:disposition",
        status=final_status,
    )
    _persist_session(session)
    return {
        "dossier": payload,
        "resolution": resolution,
        "procedure_memory_id": procedure_memory_id,
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
        risk = "readonly" if (command and _is_safe_probe(command)) else ("gated" if claimed != "auto" else "auto")
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
                "citations": [], "evidence": [],
                "hypothesis_state": _hypothesis_view(session),
                "probe_rounds": session.probe_rounds, "degraded": True}

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
                + "\n本次调查检索结果（历史线索与参考知识）：\n"
                + f"{json.dumps(_retrieval_context(session), ensure_ascii=False)}\n"
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
    turn_number = len(session.turns)
    _append_case_event(
        session,
        "investigation_turn_completed",
        {
            "sessionId": session.session_id,
            "turn": turn_number,
            "citations": citations,
            "freshEvidenceIds": [item.get("evidence_id") for item in fresh if item.get("evidence_id")],
        },
        event_id=f"{session.session_id}:turn:{turn_number}",
        status="investigating",
    )
    _persist_session(session)
    return {
        "answer": answer,
        "citations": citations,
        "evidence": fresh,
        "hypothesis_state": _hypothesis_view(session),
        "probe_rounds": session.probe_rounds,
        "memory_commit": _archive_confirmed_if_complete(session),
        "degraded": False,
    }


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
