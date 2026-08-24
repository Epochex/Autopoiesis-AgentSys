from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import AwareDatetime, BaseModel, Field, IPvAnyAddress

from .config import Settings
from .live import analyze_graph, assess_device, assess_mesh, assess_subnet, assess_wan, event_rate, subnet_graph
from .providers import list_providers
from .rca_reader import load_rca_snapshot, _load_topology
from domains.network_rca.incidents import (
    IncidentDispositionUpdate,
    IncidentRepository,
    build_incident_replay,
    detect_dual_mac_window,
    detect_host_network_drift,
    detect_host_network_preflight,
)

settings = Settings.from_env()
_CACHE_TTL_SEC = 5.0
_cache_lock = asyncio.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_loaded_at = 0.0
_evolving_service = None
_runtime_error: str | None = None
_operational_memory = None
_operational_stop = None
_diagnosis_cases: dict[str, Any] = {}
_PRODUCTION_MEMORY_BUDGET = 64
_PRODUCTION_CONSOLIDATION_OPTIONS = {"resolve_conflicts": True}
# The environment sweep reads the whole syslog corpus, so it is cached for
# longer than the live snapshot; ?refresh=1 forces a re-sweep after a source or
# ARP snapshot changes.
_ENVIRONMENT_TTL_SEC = 300.0
_environment_lock = asyncio.Lock()
_environment_report: dict[str, Any] | None = None
_environment_loaded_at = 0.0
_incident_repository = IncidentRepository(
    ledger_path=settings.incident_disposition_ledger_path
)


def _configure_production_memory_budget() -> None:
    # The store starts with five protected priors. A cap of 64 leaves room for
    # 59 learned records at today's single-digit scale and still bounds growth.
    # The service fallback is unbounded, so the gateway supplies this default
    # before constructing the production service while preserving operator overrides.
    os.environ.setdefault(
        "AUTOPOIESIS_MEMORY_BUDGET",
        str(_PRODUCTION_MEMORY_BUDGET),
    )


class RCADiagnosisRequest(BaseModel):
    """Select a server-validated case without accepting client-authored skills."""

    case_id: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)


class IncidentAssetResponse(BaseModel):
    ip: str
    name: str


class IncidentReadbackResponse(BaseModel):
    state: str
    source_kind: str | None
    collected_at: str | None
    checks: list[dict[str, Any]]


class IncidentDispositionResponse(BaseModel):
    status: str
    operator_note: str | None
    updated_at: str | None
    readback: IncidentReadbackResponse


class IncidentEvidenceResponse(BaseModel):
    sequence: int
    occurred_at: str | None
    source_kind: Literal["observed", "manual", "readback", "simulated"]
    source_type: str
    evidence_locator: str
    evidence_id: str
    phase: str
    summary: str
    facts: dict[str, Any]


class IncidentSummaryResponse(BaseModel):
    id: str
    title: str
    incident_type: str
    severity: str
    asset: IncidentAssetResponse
    classification: Literal["historical_real_incident"]
    summary: str
    disposition: IncidentDispositionResponse
    current_online_observation: Literal[False]
    evidence_count: int


class IncidentDetailPayload(BaseModel):
    id: str
    title: str
    incident_type: str
    severity: str
    asset: IncidentAssetResponse
    classification: Literal["historical_real_incident"]
    current_online_observation: Literal[False]
    summary: str
    root_cause: str
    impact: str
    root_cause_evidence_ids: list[str]
    captured_at: str | None
    window: dict[str, Any]
    evidence_timeline: list[IncidentEvidenceResponse]
    detector: dict[str, Any]
    replay_signals: list[dict[str, Any]]
    disposition: IncidentDispositionResponse
    detection: dict[str, Any]
    capturedAt: str | None
    rootCause: str
    rootCauseEvidenceIds: list[str]
    metrics: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    topology: dict[str, Any]
    response: dict[str, Any]


class IncidentListResponse(BaseModel):
    ok: Literal[True]
    live: Literal[False]
    dataMode: Literal["historical_fixture"]
    datasetKind: str
    currentOnlineObservation: Literal[False]
    count: int
    incidents: list[IncidentSummaryResponse]
    note: str


class IncidentDetailResponse(BaseModel):
    ok: Literal[True]
    live: Literal[False]
    dataMode: Literal["historical_fixture"]
    currentOnlineObservation: Literal[False]
    incident: IncidentDetailPayload


class IncidentDispositionAPIResponse(BaseModel):
    ok: Literal[True]
    persistence: Literal["append_only_jsonl"]
    currentOnlineObservation: Literal[False]
    incident: IncidentDetailPayload


class IncidentReplayResponse(BaseModel):
    ok: Literal[True]
    live: Literal[False]
    topic: str
    streamed: dict[str, Any] | None
    topicStatus: dict[str, Any] | None
    incident_id: str
    replay: Literal[True]
    source_kind: Literal["simulated"]
    time_basis: Literal["simulated_relative_clock"]
    current_online_observation: Literal[False]
    events: list[dict[str, Any]]
    detection: dict[str, Any]


class DualMacObservationRequest(BaseModel):
    event_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    ip: IPvAnyAddress
    mac: str = Field(pattern=r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    source_kind: Literal["observed", "manual", "readback", "simulated"] = "observed"


class DualMacDetectionRequest(BaseModel):
    events: list[DualMacObservationRequest] = Field(min_length=2)
    window_seconds: int = Field(default=300, ge=1, le=86400)


class NetworkPreflightDetectionRequest(BaseModel):
    expected_business_ips: list[IPvAnyAddress] = Field(min_length=1)
    cloud_init_enabled: bool
    cloud_init_network_disabled: bool
    persistent_business_ips: list[IPvAnyAddress]
    actual_business_ips: list[IPvAnyAddress] | None = None
    source_kind: Literal["observed", "manual", "readback", "simulated"] = "observed"


class IncidentDetectionResponse(BaseModel):
    ok: Literal[True]
    readonly: Literal[True]
    source_kind: str
    currentOnlineObservation: Literal[False]
    detection: dict[str, Any]
    actionPlan: dict[str, Any]


def _start_prewarm() -> None:
    """Warm the subnet models so the first UI hit is a cache hit — off by default.

    This costs one paid model call per subnet per language on *every* process
    start, and the cache it fills is in-process, so a restart pays again. With
    push-to-deploy restarting the service on every commit, that turned a
    convenience into a per-commit charge that nothing logged and nothing
    surfaced — failures here are swallowed by design.

    Set AUTOPOIESIS_PREWARM=1 to enable it on a long-lived deployment where the
    process is not restarted often. The only thing lost by leaving it off is
    that the first click on a subnet waits for the model instead of reading a
    cache.
    """
    import threading

    if os.getenv("AUTOPOIESIS_PREWARM", "0") != "1":
        return

    from .rca_reader import _load_meshes

    def warm() -> None:
        for cidr in (_load_meshes() or {}):
            for lang in ("zh", "en"):
                try:
                    assess_mesh(cidr, lang)
                except Exception:
                    # Best-effort prewarm; a cold cache only means the first UI hit is slower.
                    pass

    threading.Thread(target=warm, daemon=True).start()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _diagnosis_cases, _evolving_service, _runtime_error, _operational_memory, _operational_stop
    _start_prewarm()
    _configure_production_memory_budget()
    from .operational_memory import build_operational_memory_service

    _operational_memory = await asyncio.to_thread(build_operational_memory_service)
    try:
        from domains.network_rca.factory import build_network_rca_service
        from domains.network_rca.real_dataset import (
            load_real_case_bundle,
            resolve_stats_path,
            validate_real_dataset_manifest,
        )
        from .rca_reader import _MANIFEST

        validation = validate_real_dataset_manifest(_MANIFEST)
        if validation.ready:
            cases, _ground_truth = load_real_case_bundle(_MANIFEST, split="heldout")
            _diagnosis_cases = {case.id: case for case in cases}
            _evolving_service = await asyncio.to_thread(
                build_network_rca_service,
                settings.trace_ledger_path,
                data_source="real",
                real_stats_path=resolve_stats_path(_MANIFEST),
                reasoner_mode="rule",
                knowledge_corpus_path=settings.knowledge_corpus_path,
                adaptive_multiagent_enabled=True,
                adaptive_options={
                    "max_rounds": 2,
                    "planner_batch_size": 4,
                    "max_parallel_agents": 4,
                    "reject_on_insufficient_evidence": True,
                },
                # This is the diagnose production entry point. Passing the
                # option here activates the existing supersession branch when
                # a later diagnosis changes the root for the same subject.
                consolidation_options=dict(_PRODUCTION_CONSOLIDATION_OPTIONS),
                raise_on_evolution_error=False,
            )
            _runtime_error = None
        else:
            _runtime_error = "validated RCA dataset is unavailable"
    except Exception as exc:
        _runtime_error = f"{type(exc).__name__}: {exc}"

    # Start source refresh and the autonomous detector loop after both memory
    # services exist, so their first completed event can be persisted instead
    # of racing gateway construction.
    import threading
    from .sentinel_wiring import start_background

    stop_event = threading.Event()
    _operational_stop = stop_event

    def refresh_operational_memory() -> None:
        interval = max(30.0, float(os.getenv("AUTOPOIESIS_OPERATIONAL_REFRESH_INTERVAL", "300")))
        while not stop_event.is_set():
            _operational_memory.refresh()
            stop_event.wait(interval)

    threading.Thread(target=refresh_operational_memory, daemon=True).start()
    start_background()
    try:
        yield
    finally:
        if _operational_stop is not None:
            _operational_stop.set()
        if _evolving_service is not None:
            await asyncio.to_thread(_evolving_service.close)
        _diagnosis_cases = {}
        _operational_memory = None
        _operational_stop = None


app = FastAPI(
    title="Autopoiesis Network RCA Console",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=_lifespan,
)

# The evolution observatory ships every memory record + lifecycle event (~290 KB of
# highly repetitive JSON); it gzips to ~11 KB.
app.add_middleware(GZipMiddleware, minimum_size=1024)

if settings.cors_origins:
    allow_all = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else list(settings.cors_origins),
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    if _evolving_service is None:
        return {"status": "degraded", "runtimeError": _runtime_error}
    runtime = _evolving_service.health()
    memory_records = _evolving_service.memory.records()
    sentinel_records = sum(
        1
        for record in memory_records
        if any(str(trace_id).startswith("sentinel") for trace_id in record.source_trace_ids)
    )
    return {
        "status": "ok" if runtime.get("last_error") is None else "degraded",
        "durableMemory": _evolving_service.memory.repository is not None,
        "runtime": runtime,
        "memorySystem": {
            # The legacy counter belongs only to explicit validated-diagnosis runs.
            # Sentinel incident learning uses a separate ingestion path and is
            # reported independently so a zero here cannot hide live memories.
            "diagnosisRunConsolidations": runtime.get("consolidations", 0),
            "records": len(memory_records),
            "sentinelIncidentRecords": sentinel_records,
            "operationalObjects": (
                _operational_memory.health_view()
                if _operational_memory is not None else None
            ),
        },
    }


_cache_provider = "rule"


async def _get_snapshot(provider: str = "rule", force: bool = False) -> dict[str, Any]:
    global _cache_payload, _cache_loaded_at, _cache_provider
    now = time.monotonic()
    fresh = _cache_payload is not None and now - _cache_loaded_at <= _CACHE_TTL_SEC
    if not force and fresh and provider == _cache_provider:
        return _cache_payload
    async with _cache_lock:
        now = time.monotonic()
        fresh = _cache_payload is not None and now - _cache_loaded_at <= _CACHE_TTL_SEC
        if not force and fresh and provider == _cache_provider:
            return _cache_payload
        payload = await asyncio.to_thread(load_rca_snapshot, None, provider)
        _cache_payload = payload
        _cache_loaded_at = time.monotonic()
        _cache_provider = provider
        return payload


@app.get("/api/rca/providers")
def rca_providers() -> dict[str, Any]:
    return {"providers": list_providers()}


@app.get("/api/rca/pulse")
async def rca_pulse() -> dict[str, Any]:
    return await asyncio.to_thread(event_rate)


@app.get("/api/rca/threat")
async def rca_threat(ip: str, cidr: str = "", lang: str = "zh") -> dict[str, Any]:
    topo = _load_topology() or {}
    device, peers = None, []
    for sub in topo.get("subnets", []):
        if cidr and sub.get("cidr") != cidr:
            continue
        for dv in sub.get("devices", []) or []:
            if dv.get("ip") == ip:
                device, cidr, peers = dv, sub.get("cidr", cidr), (sub.get("devices") or [])
                break
        if device:
            break
    if device is None and cidr:
        # Every host on the segment is analyzable now, not just the handful the old
        # topology fixture carried — fall back to the full mined device graph.
        g = subnet_graph(cidr)
        if g.get("ok"):
            devs = g["devices"]
            hit = next((d for d in devs if d["ip"] == ip), None)
            if hit is not None:
                device = {**hit, "top_ports": hit.get("topPorts", [])}
                peers = [
                    {**p, "top_ports": p.get("topPorts", [])}
                    for p in sorted(devs, key=lambda d: -(d["deny"] + d["flows"]))[:9]
                ]
    if device is None:
        return {"ok": False, "text": "device not found"}
    return await asyncio.to_thread(assess_device, ip, cidr, device, lang, peers)


@app.get("/api/rca/wan_threat")
async def rca_wan_threat(ip: str, lang: str = "zh") -> dict[str, Any]:
    return await asyncio.to_thread(assess_wan, ip, lang)


@app.get("/api/rca/device_profile")
async def rca_device_profile(ip: str, lang: str = "zh") -> dict[str, Any]:
    """Traffic portrait for one host: destinations, inbound sources, activity, inferred tags."""
    from .device_profile import device_profile
    return await asyncio.to_thread(device_profile, ip, lang)


@app.get("/api/rca/device_history")
async def rca_device_history(ip: str, days: int = 7, full: int = 0) -> dict[str, Any]:
    """Historical portrait for one host over the last N days, from ClickHouse netops.facts."""
    from .history import device_history
    out = await asyncio.to_thread(device_history, ip, days, bool(full))
    return out if out is not None else {"ok": False, "text": "history store unavailable"}


@app.get("/api/rca/vlan_diag")
async def rca_vlan_diag(cidr: str, days: int = 7) -> dict[str, Any]:
    """Passive /24 congestion/pressure aggregate from ClickHouse (NOT latency)."""
    from .vlan_diag import vlan_diag
    out = await asyncio.to_thread(vlan_diag, cidr, days)
    return out if out is not None else {"ok": False, "text": "history store unavailable"}


@app.get("/api/rca/probe_latency")
async def rca_probe_latency(ip: str = "", cidr: str = "", count: int = 5) -> dict[str, Any]:
    """Active on-demand ICMP round-trip probe (real RTT) for one host or a /24."""
    from .vlan_diag import _prefix24, probe_latency

    targets: list[str] = []
    if ip:
        targets = [ip]
    elif cidr:
        prefix = _prefix24(cidr)
        if prefix is not None:
            from .rca_reader import _load_device_graphs

            graphs = await asyncio.to_thread(_load_device_graphs)
            for g in graphs.values():
                if g.get("cidr") != cidr:
                    continue
                for dv in g.get("devices", []):
                    dip = dv.get("ip")
                    if dip and str(dip).startswith(prefix):
                        targets.append(str(dip))
            targets = targets[:24]
    else:
        return {"ok": False, "text": "need ip or cidr"}
    return await asyncio.to_thread(probe_latency, targets, count)


@app.get("/api/rca/attack_surface")
async def rca_attack_surface() -> dict[str, Any]:
    # Resident deep attack-surface analysis: full real held-out WAN evidence
    # (brute-force funnel, /24 attacker netblocks, deny ports, Dahua device probes).
    from .live import wan_attack_surface
    return await asyncio.to_thread(wan_attack_surface)


@app.get("/api/rca/threat_subnet")
async def rca_threat_subnet(cidr: str, lang: str = "zh") -> dict[str, Any]:
    return await asyncio.to_thread(assess_subnet, cidr, lang)


@app.get("/api/rca/mesh_analyze")
async def rca_mesh_analyze(cidr: str, lang: str = "zh") -> dict[str, Any]:
    return await asyncio.to_thread(assess_mesh, cidr, lang)


@app.get("/api/rca/subnet_graph")
async def rca_subnet_graph(cidr: str) -> dict[str, Any]:
    """Every host on the segment plus the device<->device relations mined from syslog."""
    return await asyncio.to_thread(subnet_graph, cidr)


@app.get("/api/rca/graph_analyze")
async def rca_graph_analyze(cidr: str, lang: str = "zh") -> dict[str, Any]:
    """Agent pass over the device graph: community names, hidden patterns, pivot corridors."""
    return await asyncio.to_thread(analyze_graph, cidr, lang)


@app.get("/api/rca/snapshot")
async def rca_snapshot(provider: str = "rule", refresh: bool = False) -> dict[str, Any]:
    payload = await _get_snapshot(provider=provider, force=refresh)
    if not payload.get("datasetReady"):
        return payload
    return {
        **payload,
        "note": (
            "FortiGate/R230 留出集，采集窗口 2026-06-16 至 2026-06-17"
            f"（推理器：{payload.get('reasonerMode', 'unknown')}） / "
            "Held-out FortiGate/R230 capture from 2026-06-16 through 2026-06-17"
            f" (reasoner: {payload.get('reasonerMode', 'unknown')})."
        ),
    }


@app.post("/api/rca/diagnose")
async def rca_diagnose(request: RCADiagnosisRequest) -> dict[str, Any]:
    """Run the long-lived verified diagnosis and learning path without notifications."""
    if _evolving_service is None:
        raise HTTPException(
            status_code=503,
            detail=_runtime_error or "evolving runtime is unavailable",
        )
    case = _diagnosis_cases.get(request.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="unknown validated RCA case_id")
    diagnosis, report = await asyncio.to_thread(
        _evolving_service.diagnose,
        case,
        session_id=request.session_id,
    )
    consolidation = _evolving_service.last_consolidation
    evolution_findings = [
        event.payload
        for event in _evolving_service._run_events
        if event.kind == "system_evolution_analyzed"
    ]
    runtime = _evolving_service.health()
    memory_committed = consolidation is not None and runtime.get("last_error") is None
    return {
        "ok": report.passed and memory_committed,
        "overallStatus": "ok" if report.passed and memory_committed else "partial_failure",
        "diagnosisVerified": report.passed,
        "memoryCommitted": memory_committed,
        "runId": _evolving_service.last_run_id,
        "sessionId": request.session_id,
        "diagnosis": diagnosis.model_dump(mode="json"),
        "verification": report.model_dump(mode="json"),
        "consolidation": asdict(consolidation) if consolidation is not None else None,
        "systemEvolution": evolution_findings,
        "runtime": runtime,
    }


@app.get("/api/rca/observability/traces")
def rca_observation_traces(
    limit: int = Query(default=50, ge=1, le=500),
    session_id: str | None = None,
) -> dict[str, Any]:
    """Recent complete or interrupted online trajectories with bottlenecks."""
    if _evolving_service is None:
        raise HTTPException(status_code=503, detail="evolving runtime is unavailable")
    from core.observability import TraceAnalyzer

    traces = TraceAnalyzer(_evolving_service.observer.ledger).recent(
        limit=limit, session_id=session_id
    )
    return {
        "traces": [trace.model_dump(mode="json", exclude={"nodes"}) for trace in traces],
        "observability": _evolving_service.observer.health(),
    }


@app.get("/api/rca/observability/traces/{trace_id}")
def rca_observation_trace(trace_id: str) -> dict[str, Any]:
    """Every parent/child node and metric for one run."""
    if _evolving_service is None:
        raise HTTPException(status_code=503, detail="evolving runtime is unavailable")
    from core.observability import TraceAnalyzer

    try:
        trace = TraceAnalyzer(_evolving_service.observer.ledger).trace(trace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return trace.model_dump(mode="json")


@app.get("/api/rca/observability/sessions/{session_id}")
def rca_observation_session(session_id: str) -> dict[str, Any]:
    """Cross-run node performance and evolution for one long-lived session."""
    if _evolving_service is None:
        raise HTTPException(status_code=503, detail="evolving runtime is unavailable")
    from core.observability import TraceAnalyzer

    return TraceAnalyzer(_evolving_service.observer.ledger).session(session_id)


_MEMORY_TIERS = ("episodic", "semantic", "procedural", "asset_profile")
_MEMORY_TEXT_LIMIT = 240
_MEMORY_EVENT_TEXT_LIMIT = 120
_MEMORY_RETENTION_CHECKPOINT_TAG = "memory_retention_checkpoint"


def _memory_records() -> tuple[bool, list[Any], int | None, str | None]:
    """Read the durable snapshot when available, otherwise the local store."""
    service = _evolving_service
    memory = getattr(service, "memory", None) if service is not None else None
    if memory is None:
        return False, [], None, None

    repository = getattr(memory, "repository", None)
    load_records = getattr(repository, "load_records", None)
    # PostgreSQL is the source of truth across replicas. Reading it here avoids
    # presenting a process-local projection as the current durable memory state.
    if callable(load_records):
        records = load_records(include_quarantined=True)
    else:
        records = memory.records()

    records = list(records)
    retention_health: dict[str, Any] = {}
    health = getattr(service, "health", None)
    if callable(health):
        health_payload = health()
        if isinstance(health_payload, dict):
            retention_health = dict(health_payload.get("memory_retention") or {})
    budget = retention_health.get("budget")
    if isinstance(budget, bool) or not isinstance(budget, int):
        budget = None

    last_decay_at = retention_health.get("last_decay_at")
    if hasattr(last_decay_at, "isoformat"):
        last_decay_at = last_decay_at.isoformat()
    visible_records = []
    for record in records:
        if _MEMORY_RETENTION_CHECKPOINT_TAG in record.tags:
            if last_decay_at is None:
                last_decay_at = _memory_time(record.last_observed_at)
            # The checkpoint schedules retention; presenting it as remembered
            # incident content would inflate both the record and quarantine counts.
            continue
        visible_records.append(record)
    return repository is not None, visible_records, budget, last_decay_at


def _memory_time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _memory_relation(relation: Any) -> dict[str, Any]:
    if hasattr(relation, "model_dump"):
        return relation.model_dump(mode="json")
    return dict(relation)


def _memory_quarantine_reason(record: Any) -> str | None:
    for tag in reversed(record.tags):
        if tag.startswith("quarantine:"):
            return tag.removeprefix("quarantine:")
    return None


def _memory_list_record(record: Any) -> dict[str, Any]:
    return {
        "memory_id": record.memory_id,
        "tier": record.tier,
        "text": record.text[:_MEMORY_TEXT_LIMIT],
        "tags": list(record.tags),
        "asset_ids": list(record.asset_ids),
        "confidence": record.confidence,
        "importance": record.importance,
        "strength": record.strength,
        "access_count": record.access_count,
        "first_observed_at": _memory_time(record.first_observed_at),
        "last_observed_at": _memory_time(record.last_observed_at),
        "valid_from": _memory_time(record.valid_from),
        "valid_to": _memory_time(record.valid_to),
        "quarantined": record.quarantined,
        # A count proves provenance exists without exposing every trace id in a
        # collection response. The detail endpoint is the deliberate audit path.
        "source_trace_ids": len(record.source_trace_ids),
    }


def _memory_event(event: Any) -> dict[str, Any]:
    record = event.record
    return {
        "offset": event.event_offset,
        "memory_id": event.memory_id,
        "version": event.version,
        "event_type": event.event_type,
        "occurred_at": _memory_time(event.occurred_at),
        "tier": record.tier,
        "quarantine_reason": _memory_quarantine_reason(record),
        "strength": float(record.strength),
        "confidence": float(record.confidence),
        "importance": float(record.importance),
        "text_head": record.text[:_MEMORY_EVENT_TEXT_LIMIT],
    }


def _memory_detail_record(record: Any) -> dict[str, Any]:
    evidence = list(record.evidence_snapshot)
    return {
        **_memory_list_record(record),
        "text": record.text,
        "source_trace_ids": list(record.source_trace_ids),
        "evidence_ids": list(record.evidence_ids),
        "links": list(record.links),
        "relations": [_memory_relation(relation) for relation in record.relations],
        # Evidence bodies can contain raw observations. Identifiers and cardinality
        # are sufficient to audit provenance without turning this into a data leak.
        "evidence_snapshot": {
            "count": len(evidence),
            "evidence_ids": [
                item["evidence_id"]
                for item in evidence
                if item.get("evidence_id") is not None
            ],
        },
        "quarantine_reason": _memory_quarantine_reason(record),
        "superseded_by": record.superseded_by,
    }


_MEMORY_INFLUENCE_KINDS = frozenset({
    "memory_attributed",
    "memory_resolved",
    "memory_shortcut",
})


def _trace_memory_ids(event: Any) -> set[str]:
    """Read only explicit attribution fields from a decision trace event."""
    payload = event.payload
    memory_ids = {
        str(memory_id)
        for memory_id in payload.get("memory_ids", [])
        if memory_id
    }
    memory_id = payload.get("memory_id")
    if memory_id:
        memory_ids.add(str(memory_id))
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("memory_id"):
            memory_ids.add(str(item["memory_id"]))
    return memory_ids


def _memory_trace_events() -> list[Any]:
    """Replay the decision ledger without treating retrieval events as influence."""
    service = _evolving_service
    orchestrator = getattr(service, "orchestrator", None) if service is not None else None
    ledger = getattr(orchestrator, "ledger", None)
    replay = getattr(ledger, "replay", None)
    if callable(replay):
        return list(replay())

    if not settings.trace_ledger_path.exists():
        return []
    from core.trace.ledger import JSONLTraceLedger

    return JSONLTraceLedger(settings.trace_ledger_path).replay()


def _memory_influence_timeline() -> list[dict[str, Any]]:
    from core.remediate.sentinel import timeline

    return timeline(None)


def _memory_resolved_cycles(record: Any) -> set[tuple[str, str, str, str]]:
    """Identify the exact sentinel resolution rows retained by one memory."""
    cycles: set[tuple[str, str, str, str]] = set()
    for evidence in record.evidence_snapshot:
        if not isinstance(evidence, dict):
            continue
        row = evidence.get("data")
        if not isinstance(row, dict) or row.get("kind") != "resolved":
            continue
        at = str(row.get("at") or "")
        subject = str(row.get("subject") or "")
        if at and subject:
            cycles.add((
                at,
                str(row.get("detector") or ""),
                subject,
                str(row.get("action") or ""),
            ))
    return cycles


def _completed_memory_chain(
    subject: str, incident_reference: str | None = None,
) -> list[dict[str, Any]]:
    """Exact closed Sentinel chain, with subject fallback for older cards.

    The live-situation projection also selects the latest cycle for a subject.
    Using the same append-only ledger here lets the UI distinguish records
    produced by the selected incident from older memories about the same asset.
    """
    from core.remediate.sentinel import timeline
    from domains.network_rca.incident_memory import completed_incident_chains, incident_ref

    matches: list[list[dict[str, Any]]] = []
    for chain in completed_incident_chains(timeline(2000)):
        detected = next(
            (row for row in chain if row.get("kind") == "detected"),
            {},
        )
        target = str(detected.get("target") or detected.get("subject") or "")
        if target == subject and (
            incident_reference is None or incident_ref(chain) == incident_reference
        ):
            matches.append([dict(row) for row in chain])
    return matches[-1] if matches else []


def _incident_memory_trace_id(chain: list[dict[str, Any]]) -> str | None:
    """Return the exact source trace id consolidation assigns to a chain."""
    if not chain:
        return None
    from domains.network_rca.incident_memory import (
        synthesize_incident_run,
        synthesize_ineffective_memory,
        synthesize_safety_gated_memory,
    )

    passed = synthesize_incident_run(chain)
    if passed is not None:
        return passed.run_id
    ineffective = synthesize_ineffective_memory(chain)
    if ineffective is not None:
        return ineffective.run_id
    safety_gated = synthesize_safety_gated_memory(chain)
    return safety_gated.run_id if safety_gated is not None else None


def _prior_cycle_memory_index(records: list[Any]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Map a cited repair round back to the episodic record that retained it."""
    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    # A semantic or procedural record may share the source run, while the
    # episodic record alone retains the incident body. Prefer it deterministically.
    ordered = sorted(
        records,
        key=lambda record: (record.tier != "episodic", record.quarantined, record.memory_id),
    )
    for record in ordered:
        for cycle in _memory_resolved_cycles(record):
            index.setdefault(cycle, {
                "memory_id": record.memory_id,
                "tier": record.tier,
                "text": record.text,
            })
    return index


def _attach_prior_cycle_memories(
    rows: list[dict[str, Any]], records: list[Any]
) -> list[dict[str, Any]]:
    """Add real record content to escalation citations when the link exists."""
    index = _prior_cycle_memory_index(records)
    enriched: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if row.get("kind") != "escalated":
            enriched.append(row)
            continue
        cycles = []
        for source_cycle in row.get("prior_cycles", []):
            if not isinstance(source_cycle, dict):
                continue
            cycle = dict(source_cycle)
            key = (
                str(cycle.get("at") or ""),
                str(row.get("detector") or ""),
                str(row.get("subject") or ""),
                str(row.get("action") or ""),
            )
            memory = index.get(key)
            if memory is not None:
                cycle["memory"] = dict(memory)
            cycles.append(cycle)
        row["prior_cycles"] = cycles
        enriched.append(row)
    return enriched


def _trace_influence(event: Any, record: Any) -> dict[str, Any]:
    payload = event.payload
    source_trace_ids = list(record.source_trace_ids)
    evidence: dict[str, Any] = {
        "source_trace_id": event.run_id,
        "record_source_trace_ids": source_trace_ids,
    }
    subject = str(payload.get("subject") or event.case_id)

    if event.kind == "memory_shortcut":
        skills = list(payload.get("skills") or [])
        candidate_count = payload.get("candidate_probe_count")
        saved_count = payload.get("saved_probe_count")
        if (
            isinstance(candidate_count, int)
            and not isinstance(candidate_count, bool)
            and isinstance(saved_count, int)
            and not isinstance(saved_count, bool)
            and saved_count > 0
        ):
            narrowed_to = max(0, candidate_count - saved_count)
            what_changed = f"探针集从 {candidate_count} 条收窄到 {narrowed_to} 条"
        else:
            preferred = list(payload.get("preferred_probes") or skills)
            what_changed = f"将 {len(preferred)} 条历史高收益探针调整到队首"
        for key in (
            "skills", "preferred_probes", "candidate_probe_count",
            "saved_probe_count", "skipped_probes", "effect",
            "confirmed_root_key", "procedural_confidence",
            "original_probe_order", "planned_probe_order", "executed_probe_order",
        ):
            if key in payload:
                evidence[key] = payload[key]
        return {
            "kind": "probe_shortcut",
            "at": event.timestamp.isoformat(),
            "subject": subject,
            "what_changed": what_changed,
            "evidence": evidence,
        }

    if event.kind == "memory_resolved":
        fresh_probe_count = int(payload.get("fresh_probe_count") or 0)
        evidence.update({
            key: payload[key]
            for key in (
                "remembered_root_cause_key", "fresh_probe_count",
                "freshness_verified", "current_evidence_ids",
                "historical_evidence_ids", "recalled_confidence",
            )
            if key in payload
        })
        return {
            "kind": "diagnosis_resolution",
            "at": event.timestamp.isoformat(),
            "subject": subject,
            "what_changed": f"历史假设经 {fresh_probe_count} 条新探针确认",
            "evidence": evidence,
        }

    items = [
        dict(item) for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("memory_id") == record.memory_id
    ]
    roles = {str(item.get("role") or "") for item in items}
    evidence["items"] = items
    if "episodic_hypothesis" in roles:
        what_changed = "诊断采用了历史假设"
    elif "procedural_shortcut" in roles:
        what_changed = "探针选择采用了既有流程"
    else:
        what_changed = "本次决定采用了该记忆"
    return {
        "kind": "diagnosis_attribution",
        "at": event.timestamp.isoformat(),
        "subject": subject,
        "what_changed": what_changed,
        "evidence": evidence,
    }


def _memory_influences_for_record(
    record: Any,
    trace_events: list[Any],
    timeline_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """All explicitly attributable effects for one record, in time order."""
    memory_id = record.memory_id
    influences = [
        _trace_influence(event, record)
        for event in trace_events
        if event.kind in _MEMORY_INFLUENCE_KINDS
        and memory_id in _trace_memory_ids(event)
    ]

    resolved_cycles = _memory_resolved_cycles(record)
    for row in timeline_rows:
        if row.get("kind") != "escalated":
            continue
        matching_cycles = [
            dict(cycle)
            for cycle in row.get("prior_cycles", [])
            if isinstance(cycle, dict)
            and (
                str(cycle.get("at") or ""),
                str(row.get("detector") or ""),
                str(row.get("subject") or ""),
                str(row.get("action") or ""),
            ) in resolved_cycles
        ]
        if not matching_cycles:
            continue
        action = str(row.get("action") or "该动作")
        influences.append({
            "kind": "escalation",
            "at": str(row.get("at") or ""),
            "subject": str(row.get("subject") or ""),
            "what_changed": f"拒绝执行 {action}，转人工",
            "evidence": {
                "recurrences": int(row.get("recurrences") or len(row.get("prior_cycles", []))),
                "prior_cycles": [
                    dict(cycle)
                    for cycle in row.get("prior_cycles", [])
                    if isinstance(cycle, dict)
                ],
                "matching_prior_cycles": matching_cycles,
            },
        })

    influences.sort(key=lambda influence: str(influence.get("at") or ""))
    return influences


@app.get("/api/rca/memory")
async def rca_memory(
    tier: Literal["episodic", "semantic", "procedural", "asset_profile"] | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    include_quarantined: bool = False,
) -> dict[str, Any]:
    """Current online memory, separate from the deterministic benchmark replay."""
    from core.evolve.observatory import CAPABILITIES

    durable, records, configured_budget, last_decay_at = await asyncio.to_thread(
        _memory_records
    )
    active = [record for record in records if not record.quarantined]
    counts = {memory_tier: 0 for memory_tier in _MEMORY_TIERS}
    for record in active:
        counts[record.tier] += 1
    counts["quarantined"] = sum(1 for record in records if record.quarantined)

    selected = records if include_quarantined else active
    if tier is not None:
        selected = [record for record in selected if record.tier == tier]
    selected.sort(key=lambda record: (-record.importance, record.memory_id))

    return {
        "ok": True,
        "durable": durable,
        "counts": counts,
        "budget": {"configured": configured_budget, "active": len(active)},
        "retention": {
            "decay_wired": CAPABILITIES["decay_wired"],
            "eviction_wired": CAPABILITIES["eviction_wired"],
            "last_decay_at": last_decay_at,
            "wiringInspection": {
                "source": "repository_source",
                "method": "static_ast_scan",
                "evaluatedAt": "module_import",
            },
        },
        "records": [_memory_list_record(record) for record in selected[:limit]],
    }


@app.get("/api/rca/memory/events")
async def rca_memory_events(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    """Ordered durable memory events for read-only replay in the console."""
    service = _evolving_service
    memory = getattr(service, "memory", None) if service is not None else None
    repository = getattr(memory, "repository", None)
    read_events = getattr(repository, "read_events", None)
    if not callable(read_events):
        return {"ok": True, "durable": False, "events": [], "total": 0}

    # The repository owns ordering and decoding. Keeping the blocking database
    # call off the event loop lets this read-only view coexist with live requests.
    events = await asyncio.to_thread(
        read_events,
        after_offset=after,
        limit=limit,
    )
    raw_page = list(events)
    # The public replay contract currently exposes state creation/update and
    # quarantine only. Administrative deletion events belong to a separate
    # audit surface and may be introduced by the repository independently.
    page = [event for event in raw_page if event.event_type in {"UPSERT", "QUARANTINE"}]
    next_offset = raw_page[-1].event_offset if len(raw_page) == limit else None
    return {
        "ok": True,
        "durable": True,
        "total": len(page),
        "next_offset": next_offset,
        "events": [_memory_event(event) for event in page],
    }


@app.get("/api/rca/memory/{memory_id}")
async def rca_memory_detail(memory_id: str) -> dict[str, Any]:
    """Full provenance for one online memory record, with evidence bodies withheld."""
    durable, records, _configured_budget, _last_decay_at = await asyncio.to_thread(
        _memory_records
    )
    record = next((item for item in records if item.memory_id == memory_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="memory record not found")
    return {"ok": True, "durable": durable, "record": _memory_detail_record(record)}


@app.get("/api/rca/memory/{memory_id}/influence")
async def rca_memory_influence(memory_id: str) -> dict[str, Any]:
    """Decisions that explicitly recorded this memory as changing their path."""
    _durable, records, _configured_budget, _last_decay_at = await asyncio.to_thread(
        _memory_records
    )
    record = next((item for item in records if item.memory_id == memory_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="memory record not found")

    trace_events, timeline_rows = await asyncio.gather(
        asyncio.to_thread(_memory_trace_events),
        asyncio.to_thread(_memory_influence_timeline),
    )
    influences = _memory_influences_for_record(record, trace_events, timeline_rows)
    return {
        "ok": True,
        "memory_id": memory_id,
        "influences": influences,
        "note": "没有任何影响时 influences 为空数组，不要编",
    }


@app.get("/api/rca/event-memory-receipt")
async def rca_event_memory_receipt(
    subject: str = Query(min_length=1, max_length=128),
    incident_ref: str | None = Query(default=None, min_length=1, max_length=128),
) -> dict[str, Any]:
    """Join one selected live incident to its durable memory consequences.

    The response keeps three facts separate: the dossier created for the latest
    completed chain, tiered records written by that exact chain, and older
    subject memories that later changed an investigation or escalation.
    """
    durable, records, _configured_budget, _last_decay_at = await asyncio.to_thread(
        _memory_records
    )
    chain, trace_events, timeline_rows = await asyncio.gather(
        asyncio.to_thread(_completed_memory_chain, subject, incident_ref),
        asyncio.to_thread(_memory_trace_events),
        asyncio.to_thread(_memory_influence_timeline),
    )
    source_trace_id = _incident_memory_trace_id(chain)
    current_records = [
        record for record in records
        if source_trace_id and source_trace_id in record.source_trace_ids
    ]
    # The receipt is incident-scoped. Subject-wide history stays on the online
    # memory surface, where it cannot be mistaken for output of this cycle.
    related_records: list[Any] = []
    incident_closed_at = str(chain[-1].get("at") or "") if chain else ""

    def memory_row(record: Any) -> dict[str, Any]:
        influences = _memory_influences_for_record(
            record, trace_events, timeline_rows,
        )
        return {
            **_memory_list_record(record),
            "influence_count": len(influences),
            "subsequent_influence_count": sum(
                bool(incident_closed_at) and str(item.get("at") or "") > incident_closed_at
                for item in influences
            ),
            "latest_influence": influences[-1] if influences else None,
        }

    dossier_id: str | None = None
    terminal_kind: str | None = None
    if chain:
        from domains.network_rca.incident_dossier import from_sentinel_chain

        dossier_id = from_sentinel_chain(chain, source_mode="live").dossier_id
        terminal_kind = str(chain[-1].get("kind") or "")
    operational = (
        await asyncio.to_thread(
            _operational_memory.incident_receipt_view,
            subject=subject,
            dossier_id=dossier_id,
        )
        if _operational_memory is not None
        else {
            "ok": False,
            "durable": False,
            "dossiers": [],
            "risks": [],
            "features": [],
        }
    )
    exact_dossiers = list(operational.get("dossiers", []))
    current = [memory_row(record) for record in current_records]
    related = [memory_row(record) for record in related_records]
    influence_count = sum(
        int(row["influence_count"]) for row in [*current, *related]
    )
    subsequent_influence_count = sum(
        int(row["subsequent_influence_count"]) for row in current
    )

    if terminal_kind == "no_safe_action" and current:
        lifecycle = "safety_gated"
    elif subsequent_influence_count:
        lifecycle = "reused"
    elif current:
        lifecycle = "memory_committed"
    elif exact_dossiers:
        lifecycle = "dossier_recorded"
    else:
        lifecycle = "awaiting_terminal"

    return {
        "ok": True,
        "subject": subject,
        "incident_ref": incident_ref,
        "durable": durable and bool(operational.get("durable")),
        "lifecycle": lifecycle,
        "latest_incident": {
            "completed": bool(chain),
            "terminal_kind": terminal_kind,
            "dossier_id": dossier_id,
            "source_trace_id": source_trace_id,
        },
        "dossiers": exact_dossiers,
        "risks": list(operational.get("risks", [])),
        "features": list(operational.get("features", [])),
        "current_memories": current,
        "related_memories": related,
        "influence_count": influence_count,
        "subsequent_influence_count": subsequent_influence_count,
    }


@app.get("/api/rca/evolution")
async def rca_evolution(passes: int = Query(default=4, ge=1, le=64)) -> dict[str, Any]:
    from .rca_reader import load_evolution
    payload = await asyncio.to_thread(load_evolution, None, passes)
    # This route reruns a fixed held-out stream. Label it at the top level so a
    # consumer cannot mistake its evolved benchmark store for production memory.
    return {
        **payload,
        "dataMode": "offline_benchmark_replay",
        "onlineMemory": False,
        "benchmark": {
            "caseCount": int(payload.get("nCases", len(payload.get("cases", [])))),
            "passes": int(payload.get("passes", passes)),
        },
    }


@app.get("/api/rca/memory-graph")
async def rca_memory_graph(passes: int = Query(default=4, ge=1, le=64)) -> dict[str, Any]:
    # The REAL evolved memory graph from the self-evolving run (serialize_store):
    # nodes = memory records (episodic/semantic/procedural) + insight hubs, edges =
    # the real links/relations between them. A full node-link graph — the benchmark
    # 态势 analogue of the network device-relation constellation. Reshape only; every
    # node/edge comes verbatim from load_evolution's observatory.records.
    from .rca_reader import load_evolution

    def build() -> dict[str, Any]:
        d = load_evolution(None, passes)
        recs = (d.get("observatory") or {}).get("records") or []
        node_ids = {r["memory_id"] for r in recs}
        nodes: list[dict[str, Any]] = []
        for r in recs:
            tags = r.get("tags") or []
            root = next((t.split(":", 1)[1] for t in tags if t.startswith("root:")), "")
            nodes.append({
                "id": r["memory_id"], "tier": r.get("tier", "semantic"),
                "label": root or r["memory_id"],
                "text": (r.get("text") or "")[:220],
                "strength": float(r.get("strength", 0.5) or 0.0),
                "importance": float(r.get("importance", 0.0) or 0.0),
                "tags": [t for t in tags if not t.startswith("root:")][:6],
                "assets": r.get("asset_ids") or [],
            })
        # edges: dedup by unordered pair; type from a matching typed relation if any
        rel_type: dict[tuple[str, str], str] = {}
        for r in recs:
            for rel in (r.get("relations") or []):
                t = rel.get("target_id")
                if t:
                    rel_type[tuple(sorted((r["memory_id"], t)))] = rel.get("relation_type", "link")
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        extra: set[str] = set()
        for r in recs:
            for tgt in (r.get("links") or []):
                pair = tuple(sorted((r["memory_id"], tgt)))
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append({"src": r["memory_id"], "dst": tgt, "type": rel_type.get(pair, "link")})
                if tgt not in node_ids:
                    extra.add(tgt)
        for x in sorted(extra):
            is_ins = x.startswith("insight")
            nodes.append({
                "id": x, "tier": "insight" if is_ins else "semantic",
                "label": x.replace("insight-", "") if is_ins else x,
                "text": "", "strength": 0.85, "importance": 0.6,
                "tags": ["insight"] if is_ins else [], "assets": [],
            })
        mem = d.get("memory") or {}
        return {
            "ok": True, "nodes": nodes, "edges": edges,
            "stats": {"records": len(recs), "edges": len(edges),
                      "insights": mem.get("insights", 0), "by_tier": mem.get("by_tier", {})},
        }

    return await asyncio.to_thread(build)


@app.get("/api/rca/bench-snapshot")
async def rca_bench_snapshot(passes: int = Query(default=4, ge=1, le=64)) -> dict[str, Any]:
    # Benchmark 态势 in the same topology shapes (Topology + SubnetGraph), built
    # from the offline replay's evolved memory graph so the frontend reuses TopologyCanvas
    # in its DRILLED state (drillSub === graph.cidr === "mem://autopoiesis"). Reshape
    # only: every node/edge/label comes verbatim from load_evolution()'s
    # observatory.records; flows/accept/threat/x/y are clearly derived render
    # heuristics from the real strength/importance scores (see build_bench_snapshot).
    from .rca_reader import build_bench_snapshot
    return await asyncio.to_thread(build_bench_snapshot, passes)


@app.get("/api/rca/replay")
async def rca_replay(
    lang: str = "zh",
    passes: int = Query(default=4, ge=1, le=64),
    inject: int = Query(default=0, ge=0, le=1),
) -> dict[str, Any]:
    # Offline fault-injection self-heal benchmark over six held-out cases. inject=1
    # additionally attempts to publish representative replay:true bursts to the
    # isolated topic; `streamed` reports that attempt separately from the replay.
    from core.evolve.replay_stream import (
        REPLAY_TOPIC,
        produce_replay,
        replay_trajectory,
        topic_status,
    )

    streamed = await asyncio.to_thread(produce_replay) if inject == 1 else None
    status = await asyncio.to_thread(topic_status, REPLAY_TOPIC)
    trajectory = await asyncio.to_thread(replay_trajectory, passes, lang)
    return {
        "ok": True,
        "live": False,
        "dataMode": "offline_benchmark_replay",
        "onlineMemory": False,
        "topic": REPLAY_TOPIC,
        "topic_events": status.get("events"),
        "streamed": streamed,
        **trajectory,
    }


@app.post(
    "/api/rca/incidents/detect/dual-mac",
    response_model=IncidentDetectionResponse,
)
async def rca_detect_dual_mac(request: DualMacDetectionRequest) -> dict[str, Any]:
    """Evaluate supplied ARP observations without collecting or changing host state."""
    events = [event.model_dump(mode="json") for event in request.events]
    source_kinds = sorted({event.source_kind for event in request.events})
    detection = await asyncio.to_thread(
        detect_dual_mac_window, events, request.window_seconds
    )
    return {
        "ok": True,
        "readonly": True,
        "source_kind": source_kinds[0] if len(source_kinds) == 1 else "mixed",
        "currentOnlineObservation": False,
        "detection": detection,
        "actionPlan": {
            "approvalRequired": True,
            "steps": [
                "Confirm switch-port and DHCP ownership for every detected MAC.",
                "Quarantine or readdress the unintended owner after operator approval.",
            ],
            "readbackRequirements": [
                "Observe one stable MAC for the IP across a complete detection window.",
                "Verify reachability from the affected network segment.",
            ],
        },
    }


@app.post(
    "/api/rca/incidents/detect/network-preflight",
    response_model=IncidentDetectionResponse,
)
async def rca_detect_network_preflight(
    request: NetworkPreflightDetectionRequest,
) -> dict[str, Any]:
    """Check restart persistence and optional post-change drift without remediation."""
    expected_business_ips = [str(ip) for ip in request.expected_business_ips]
    persistent_business_ips = [str(ip) for ip in request.persistent_business_ips]
    preflight = await asyncio.to_thread(
        detect_host_network_preflight,
        expected_business_ips=expected_business_ips,
        cloud_init_enabled=request.cloud_init_enabled,
        cloud_init_network_disabled=request.cloud_init_network_disabled,
        persistent_business_ips=persistent_business_ips,
        source_kind=request.source_kind,
    )
    drift = None
    if request.actual_business_ips is not None:
        drift = await asyncio.to_thread(
            detect_host_network_drift,
            expected_business_ips=expected_business_ips,
            actual_business_ips=[str(ip) for ip in request.actual_business_ips],
            source_kind=request.source_kind,
        )
    detection = {
        "detector": "host_network_preflight_drift",
        "detected": preflight["detected"] or bool(drift and drift["detected"]),
        "preflight": preflight,
        "drift": drift,
        "source_kinds": [request.source_kind],
        "observation_scope": preflight["observation_scope"],
        "current_online_observation": False,
    }
    return {
        "ok": True,
        "readonly": True,
        "source_kind": request.source_kind,
        "currentOnlineObservation": False,
        "detection": detection,
        "actionPlan": {
            "approvalRequired": True,
            "steps": [
                "Disable unintended cloud-init network management.",
                "Persist the expected business IP before an approved restart.",
            ],
            "readbackRequirements": [
                "Verify the expected IP, default route, and SSH listener after restart.",
                "Run a controlled restart to verify configuration persistence.",
            ],
        },
    }


@app.get("/api/rca/incidents", response_model=IncidentListResponse)
async def rca_incidents(
    status: str | None = None,
    incident_type: str | None = None,
    asset_ip: str | None = None,
) -> dict[str, Any]:
    """List historical incident records without implying a current live finding."""
    incidents = await asyncio.to_thread(
        _incident_repository.list,
        status=status,
        incident_type=incident_type,
        asset_ip=asset_ip,
    )
    metadata = _incident_repository.metadata
    return {
        "ok": True,
        "live": False,
        "dataMode": metadata["data_mode"],
        "datasetKind": metadata["dataset_kind"],
        "currentOnlineObservation": False,
        "count": len(incidents),
        "incidents": incidents,
        "note": metadata["note"],
    }


@app.get("/api/rca/incidents/{incident_id}", response_model=IncidentDetailResponse)
async def rca_incident_detail(incident_id: str) -> dict[str, Any]:
    """Return the evidence timeline, detector result, disposition, and readback."""
    try:
        incident = await asyncio.to_thread(_incident_repository.get, incident_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown incident_id") from exc
    return {
        "ok": True,
        "live": False,
        "dataMode": "historical_fixture",
        "currentOnlineObservation": False,
        "incident": incident,
    }


@app.patch(
    "/api/rca/incidents/{incident_id}/disposition",
    response_model=IncidentDispositionAPIResponse,
)
async def rca_incident_disposition(
    incident_id: str, request: IncidentDispositionUpdate
) -> dict[str, Any]:
    """Append a handling-state update; resolving requires a passed readback."""
    try:
        incident = await asyncio.to_thread(
            _incident_repository.update_disposition, incident_id, request
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown incident_id") from exc
    return {
        "ok": True,
        "persistence": "append_only_jsonl",
        "currentOnlineObservation": False,
        "incident": incident,
    }


@app.post("/api/rca/incidents/{incident_id}/replay", response_model=IncidentReplayResponse)
async def rca_incident_replay(
    incident_id: str,
    inject: int = Query(default=0, ge=0, le=1),
) -> dict[str, Any]:
    """Replay one incident locally or through the existing isolated replay topic."""
    from core.evolve.replay_stream import (
        REPLAY_TOPIC,
        produce_tagged_replay,
        topic_status,
    )

    try:
        replay = await asyncio.to_thread(
            build_incident_replay, _incident_repository, incident_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown incident_id") from exc

    streamed = None
    status = None
    if inject == 1:
        streamed = await asyncio.to_thread(
            produce_tagged_replay,
            replay["events"],
            [incident_id],
            topic=REPLAY_TOPIC,
        )
        status = await asyncio.to_thread(topic_status, REPLAY_TOPIC)
    return {
        "ok": True,
        "live": False,
        "topic": REPLAY_TOPIC,
        "streamed": streamed,
        "topicStatus": status,
        **replay,
    }


@app.get("/api/rca/environment")
async def rca_environment(refresh: int = Query(default=0, ge=0, le=1)) -> dict[str, Any]:
    """Environment perception sweep: what the sources prove, and what they cannot.

    Returns findings *and* the sensor-coverage matrix. The matrix is not
    decoration: a fault class with no source present is reported as ``blind``
    so an empty findings list can never be misread as a healthy segment.
    """
    global _environment_report, _environment_loaded_at
    from domains.network_rca.environment import build_environment_report

    async with _environment_lock:
        stale = time.monotonic() - _environment_loaded_at > _ENVIRONMENT_TTL_SEC
        if refresh == 1 or _environment_report is None or stale:
            _environment_report = await asyncio.to_thread(build_environment_report)
            _environment_loaded_at = time.monotonic()
        return _environment_report


class OwnershipProbeRequest(BaseModel):
    """Operator-triggered verification of one contested address.

    The address must already appear in a segment the sweep knows about, and the
    ports come from the module's allowlist. Without both limits this endpoint is
    a port scanner that anyone who can reach the console can aim at the LAN.
    """

    ip: IPvAnyAddress
    ports: list[int] | None = Field(default=None, max_length=6)
    samples: int = Field(default=16, ge=2, le=40)


@app.post("/api/rca/environment/probe")
async def rca_environment_probe(request: OwnershipProbeRequest) -> dict[str, Any]:
    """Run the read-only differential ownership probe against one address.

    Ping cannot settle a duplicate address -- both claimants answer it. This
    samples which MAC owns the ARP entry alongside a small allowlisted port set,
    so a changing service profile shows connections landing on different
    machines, and quantifies how often the intended service is unreachable.
    """
    from domains.network_rca.ownership_probe import DEFAULT_PORTS, probe_address_ownership

    target = str(request.ip)
    report = await rca_environment()
    known = set(report["totals"].get("served_segments") or [])
    segment = ".".join(target.split(".")[:3]) + ".0/24"
    if segment not in known:
        raise HTTPException(
            status_code=400,
            detail=f"address is outside the segments this sweep covers: {sorted(known)}",
        )

    try:
        result = await asyncio.to_thread(
            probe_address_ownership,
            target,
            ports=tuple(request.ports) if request.ports else DEFAULT_PORTS,
            samples=request.samples,
            interval=0.8,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "live": True, **result}


class ProbeStepRequest(BaseModel):
    """Run one read-only verification step and return its real output.

    ``command`` is not trusted caller text: it must byte-for-byte match a
    read-only step the server itself put in a current playbook. That check plus
    the executor's own parse/allowlist means the endpoint can only run probes
    the platform already chose to display, against addresses it already covers.
    """

    command: str = Field(min_length=1, max_length=400)


def _readonly_playbook_commands() -> set[str]:
    """Every read-only command currently displayed in a playbook, env + recon."""
    commands: set[str] = set()

    report = _environment_report
    if report is not None:
        for finding in report.get("findings", []):
            for step in finding.get("playbook", {}).get("verify", []):
                if step.get("risk") == "readonly" and step.get("command"):
                    commands.add(step["command"])

    try:
        from domains.active_recon.pentest import build_pentest_report

        for scenario in ("live", "bench"):
            pentest = build_pentest_report("en", "auto", scenario)
            for finding in pentest.get("findings", []):
                for step in (finding.get("playbook") or {}).get("steps", []):
                    if step.get("mode") == "readonly" and step.get("cmd"):
                        commands.add(step["cmd"])
    except Exception:
        pass
    return commands


@app.post("/api/rca/environment/run-step")
async def rca_run_step(request: ProbeStepRequest) -> dict[str, Any]:
    """Execute one non-mutating playbook step and return the real result.

    Read-only probes (tcp connect, banner read, HTTP HEAD, TLS certificate) are
    safe to run for the operator instead of asking them to copy-paste. Mutating
    or intrusive steps have no executor and are refused before anything runs.
    """
    from domains.active_recon.probe_exec import run_probe

    if _environment_report is None:
        await rca_environment()
    allowed = await asyncio.to_thread(_readonly_playbook_commands)
    if request.command not in allowed:
        raise HTTPException(
            status_code=400,
            detail="command does not match a current read-only playbook step",
        )

    report = _environment_report or {}
    served = set(report.get("totals", {}).get("served_segments") or [])

    def _in_scope(ip: str) -> bool:
        # No sweep yet (empty served set) → fall back to RFC1918, which the
        # executor already enforces; never widen to public addresses.
        segment = ".".join(ip.split(".")[:3]) + ".0/24"
        return not served or segment in served

    result = await asyncio.to_thread(run_probe, request.command, in_scope=_in_scope)
    status = 200 if result.get("ran") else 422
    if status != 200:
        return {"ok": False, "live": True, **result}
    return {"ok": True, "live": True, **result}


@app.get("/api/rca/device_search")
async def rca_device_search(q: str, limit: int = Query(default=14, ge=1, le=50)) -> dict[str, Any]:
    # Keyword search over every mined device across the topology's subnets: match
    # ip / name / vendor / role / os / top-ports / live hostname. Returns the ip +
    # cidr so the console can drill to the segment and focus the device (its
    # profile/画像). Read-only; reuses the cached subnet_graph builder.
    needle = (q or "").strip().lower()
    if not needle:
        return {"ok": True, "q": q, "results": []}

    def search() -> list[dict[str, Any]]:
        topo = _load_topology() or {}
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for sub in topo.get("subnets", []):
            cidr = sub.get("cidr")
            if not cidr:
                continue
            g = subnet_graph(cidr)
            if not g.get("ok"):
                continue
            for dv in g.get("devices", []) or []:
                ident = dv.get("liveIdentity") or {}
                hay = " ".join(str(x) for x in (
                    dv.get("ip"), dv.get("name"), dv.get("vendor"), dv.get("role"),
                    dv.get("os"), ident.get("hostname"), ident.get("type"),
                    " ".join(dv.get("topPorts") or []),
                )).lower()
                if needle in hay:
                    key = (dv.get("ip"), cidr)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "ip": dv.get("ip"), "cidr": cidr,
                        "name": dv.get("name") or ident.get("hostname"),
                        "vendor": dv.get("vendor"), "role": dv.get("role"),
                        "threat": dv.get("threat", "ok"),
                        "flows": dv.get("flows", 0), "topPorts": (dv.get("topPorts") or [])[:4],
                    })
        # most-active / highest-threat first
        rank = {"high": 0, "watch": 1, "ok": 2}
        out.sort(key=lambda d: (rank.get(d["threat"], 3), -int(d.get("flows") or 0)))
        return out[:limit]

    results = await asyncio.to_thread(search)
    return {"ok": True, "q": q, "results": results}


@app.get("/api/rca/pentest")
async def rca_pentest(lang: str = "zh", scenario: str = "live") -> dict[str, Any]:
    # Scenario-aware self-pentest report. scenario="live" (default) is the read-only
    # recon of the REAL internal-network surface; scenario="bench" is the REAL ITBench
    # CISO compliance-scenario CATALOG (published definitions only — local scoring
    # pending, see the benchmark page Task B). Intrusive weak-cred/exploit probes are
    # approval-gated and never executed; no ITBench pass/fail is fabricated.
    from domains.active_recon.pentest import build_pentest_report
    return await asyncio.to_thread(build_pentest_report, lang, "auto", scenario)


@app.get("/api/rca/retrieval")
async def rca_retrieval(
    lang: str = "zh", q: str | None = None, scenario: str = "live",
) -> dict[str, Any]:
    # Scenario-aware trace of REAL retrieval flows. scenario="live" (default) shows
    # ONLY the internal-network memory-recall cases (real TieredMemoryStore recall);
    # scenario="bench" shows ONLY the corpus/KB hybrid pipeline (BM25 + optional dense
    # + structured-tag routes, RRF fusion, CRAG gate, optional rerank, context
    # compilation). Optional stages degrade to enabled:false honestly; never fabricated.
    from core.memory.retrieval_trace import build_retrieval_trace
    return await asyncio.to_thread(build_retrieval_trace, lang, q, scenario)


@app.get("/api/rca/benchmark")
async def rca_benchmark(lang: str = "zh") -> dict[str, Any]:
    # Benchmark-scenario data: stored LongMemEval-500 result files plus the ITBench
    # published-baseline catalog. The frontend labels these as result files and does
    # not use the payload's legacy `live` field as a source badge.
    from core.eval.benchmark_report import build_benchmark_report
    return await asyncio.to_thread(build_benchmark_report, lang)


@app.get("/api/rca/live-situation")
async def rca_live_situation(lang: str = "zh") -> dict[str, Any]:
    # Read-only snapshot of NetOps landed sink files (alerts +
    # AIOps suggestions + cluster-state). The gateway never joins the Redpanda topic;
    # the two subsystems meet only at this disk boundary. Returns empty collections
    # when the runtime dir is absent, so the panel reports no landed records.
    from .runtime_reader import load_runtime_snapshot
    from .sentinel_projection import merge_into_snapshot

    snapshot = await asyncio.to_thread(load_runtime_snapshot, settings, lang)
    # The sentinel is a separate subsystem writing a separate file. Folding its
    # chains in here is what makes a fault it just found show up on the page the
    # operator is actually looking at, instead of only in the audit trail.
    return await asyncio.to_thread(merge_into_snapshot, snapshot, lang)


@app.get("/api/rca/bench-live-situation")
async def rca_bench_live_situation(lang: str = "zh") -> dict[str, Any]:
    # Same reader as /api/rca/live-situation, pointed at the isolated benchmark directory
    # dir written by the replay side-car pipeline (correlator-replay -> alerts-sink-replay
    # -> aiops-agent-replay). Real running-pod output; the prod runtime dir is untouched.
    from .runtime_reader import load_runtime_snapshot
    base = settings.netops_runtime_dir
    replay_dir = base.parent / (base.name + "-replay")
    return await asyncio.to_thread(load_runtime_snapshot, settings, lang, replay_dir)


class RemediationRequest(BaseModel):
    """Name one allowlisted action and the single target it applies to.

    ``action`` is matched against a closed set defined server-side; ``target``
    is validated by that action's own preconditions before anything runs. The
    caller cannot express a verb the platform did not already choose to offer.
    """

    action: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=128)
    dossier_id: str | None = Field(default=None, min_length=1, max_length=160)
    failure_domain: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class RemediationControlRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


@app.get("/api/rca/operational-memory")
async def operational_memory(subject: str | None = Query(default=None, max_length=128)) -> dict[str, Any]:
    """Auditable projection of dossiers, long-run risks, and promoted features."""
    if _operational_memory is None:
        raise HTTPException(status_code=503, detail="operational memory is initializing")
    return await asyncio.to_thread(_operational_memory.audit_view, subject=subject)


@app.post("/api/rca/operational-memory/refresh")
async def operational_memory_refresh() -> dict[str, Any]:
    """Merge bounded real-source windows into durable operational records."""
    if _operational_memory is None:
        raise HTTPException(status_code=503, detail="operational memory is initializing")
    return await asyncio.to_thread(_operational_memory.refresh)


@app.get("/api/rca/remediation/actions")
async def remediation_actions() -> dict[str, Any]:
    """The closed set of actions the system may run unattended."""
    from .remediation import describe_actions

    return {"ok": True, "actions": describe_actions()}


@app.get("/api/rca/remediation/safety")
async def remediation_safety() -> dict[str, Any]:
    """Current pause switch, rolling budgets, locks and observation limits."""
    from .remediation import safety_status

    return {"ok": True, **await asyncio.to_thread(safety_status)}


@app.post("/api/rca/remediation/pause")
async def remediation_pause(request: RemediationControlRequest) -> dict[str, Any]:
    """Stop all subsequent writes through a process-independent control file."""
    from .remediation import emergency_stop

    state = await asyncio.to_thread(emergency_stop().pause, request.reason, request.actor)
    return {"ok": True, "emergency_stop": state.to_dict()}


@app.post("/api/rca/remediation/resume")
async def remediation_resume(request: RemediationControlRequest) -> dict[str, Any]:
    """Resume writes with an auditable operator identity and reason."""
    from .remediation import emergency_stop

    state = await asyncio.to_thread(emergency_stop().resume, request.actor, request.reason)
    return {"ok": True, "emergency_stop": state.to_dict()}


@app.post("/api/rca/remediation/preflight")
async def remediation_preflight(request: RemediationRequest) -> dict[str, Any]:
    """Report whether an action would run, and why, without touching anything.

    A refusal is the expected answer most of the time — a healthy interface, a
    running unit, a spent restart budget — and is returned with its reason at
    200 rather than as an error, because "not eligible" is information, not a
    fault.
    """
    from .remediation import preflight

    result = await asyncio.to_thread(preflight, request.action, request.target)
    return {"ok": True, **result}


@app.post("/api/rca/remediation/execute")
async def remediation_execute(request: RemediationRequest) -> dict[str, Any]:
    """Commit an action, hold its watch window open, then pass or revert.

    Returns the whole verdict: the baseline taken before the change, every
    reading taken during the window, and what was decided. A run whose revert
    could not be proven comes back with ``needs_human`` set — that case is
    reported, never quietly closed.
    """
    from .remediation import execute

    result = await asyncio.to_thread(
        execute,
        request.action,
        request.target,
        incident_id=request.dossier_id,
        failure_domain=request.failure_domain,
        idempotency_key=request.idempotency_key,
    )
    if result.get("refused"):
        raise HTTPException(status_code=400, detail=result.get("reason", "refused"))
    dossier = None
    if request.dossier_id:
        if _operational_memory is None:
            raise HTTPException(status_code=503, detail="operational memory is initializing")
        try:
            dossier = await asyncio.to_thread(
                _operational_memory.attach_remediation_run,
                request.dossier_id,
                result,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident dossier") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
    return {"ok": True, **result, "dossier": dossier}


@app.get("/api/rca/remediation/runs")
async def remediation_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """Past runs, newest first — what the system did while nobody was watching."""
    from .remediation import history

    rows = await asyncio.to_thread(history, limit)
    return {"ok": True, "runs": rows, "count": len(rows)}


class InvestigateStart(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    family: str | None = Field(default=None, max_length=64)
    subject: str | None = Field(default=None, max_length=64)
    lang: Literal["zh", "en"] = "zh"


class InvestigateAsk(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1000)
    lang: Literal["zh", "en"] = "zh"


class InvestigateStep(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    step: int = Field(ge=1, le=99)


class InvestigateSession(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    lang: Literal["zh", "en"] = "zh"


class InvestigateClose(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    resolution: Literal["confirmed", "inconclusive", "refuted"]
    root_cause: str = Field(min_length=1, max_length=2000)
    confirmed_by: str = Field(min_length=1, max_length=128)
    evidence_ids: list[str] = Field(default_factory=list, max_length=120)
    operator_note: str | None = Field(default=None, max_length=2000)


@app.post("/api/rca/investigate/start")
async def investigate_start(request: InvestigateStart) -> dict[str, Any]:
    """Open a session by running its read-only probes, before anything reasons.

    The evidence comes back with the session id: the caller can see exactly
    what the host said before a model has been asked to interpret any of it.
    """
    from .investigate import start

    result = await asyncio.to_thread(start, request.question, request.family, request.subject)
    return {"ok": True, **result}


@app.post("/api/rca/investigate/analyze")
async def investigate_analyze(request: InvestigateSession) -> dict[str, Any]:
    """Turn the collected evidence into a diagnosis and a graded runbook."""
    from .investigate import analyze

    try:
        result = await asyncio.to_thread(analyze, request.session_id, request.lang)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    return {"ok": True, **result}


@app.post("/api/rca/investigate/ask")
async def investigate_ask(request: InvestigateAsk) -> dict[str, Any]:
    """One follow-up turn, with every earlier reading and turn still in view."""
    from .investigate import ask

    try:
        result = await asyncio.to_thread(ask, request.session_id, request.question, request.lang)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    return {"ok": True, **result}


@app.post("/api/rca/investigate/run-step")
async def investigate_run_step(request: InvestigateStep) -> dict[str, Any]:
    """Run one runbook step. Only read-only steps have an executor here."""
    from .investigate import run_step

    try:
        result = await asyncio.to_thread(run_step, request.session_id, request.step)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    return {"ok": True, **result}


@app.post("/api/rca/investigate/run-all")
async def investigate_run_all(request: InvestigateSession) -> dict[str, Any]:
    """Run the runbook in order, stopping at the first step we may not run."""
    from .investigate import run_all

    try:
        result = await asyncio.to_thread(run_all, request.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    return {"ok": True, **result}


@app.post("/api/rca/investigate/close")
async def investigate_close(request: InvestigateClose) -> dict[str, Any]:
    """Archive an explicit operator disposition and feed verified claims downstream."""
    from .investigate import close

    try:
        result = await asyncio.to_thread(
            close,
            request.session_id,
            resolution=request.resolution,
            root_cause=request.root_cause,
            confirmed_by=request.confirmed_by,
            evidence_ids=request.evidence_ids,
            operator_note=request.operator_note,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return {"ok": True, **result}


@app.get("/api/rca/investigate/{session_id}")
async def investigate_session(session_id: str) -> dict[str, Any]:
    """The whole session: every reading, every turn, the current runbook."""
    from .investigate import get

    try:
        session = await asyncio.to_thread(get, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    return {"ok": True, **session.as_dict()}


@app.get("/api/rca/cost")
async def rca_cost(hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
    """Model spend over a window, attributed to the feature that asked for it.

    Token counts come from the provider's usage object and are exact; the money
    figure is computed from a local rate table and is an estimate, so the
    response carries that caveat rather than implying it is a bill.
    """
    from core.llm.cost import summary

    return {"ok": True, **await asyncio.to_thread(summary, hours)}


@app.get("/api/rca/sentinel/timeline")
async def sentinel_timeline(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    """What the sentinel saw, decided, did and verified — oldest first.

    Includes the branches where it declined: a detection with no safe action,
    a target still in cooldown, a preflight that refused. A timeline with only
    successes in it would not be an audit trail.
    """
    from core.remediate.sentinel import timeline

    rows = await asyncio.to_thread(timeline, limit)
    if any(row.get("kind") == "escalated" for row in rows):
        _durable, records, _budget, _last_decay_at = await asyncio.to_thread(
            _memory_records
        )
        rows = _attach_prior_cycle_memories(rows, records)
    return {"ok": True, "events": rows, "count": len(rows)}


@app.get("/api/rca/sentinel/recurrence")
async def sentinel_recurrence() -> dict[str, Any]:
    """Repairs that held, then failed again, grouped by detector, subject and action."""
    from core.remediate import recurrence
    from core.remediate.sentinel import COOLDOWN_SEC, _default_timeline

    now = time.time()
    # A fixed line tail silently shortens a busy 24-hour window, so this uses
    # the ledger's time-bounded reader that backs the decision itself.
    events = await asyncio.to_thread(
        recurrence._events_since,
        _default_timeline(),
        now - recurrence.WINDOW_SEC,
    )
    histories = recurrence.project(events, now=now, window_sec=recurrence.WINDOW_SEC)

    keys = []
    for history in histories.values():
        if not history.recurrences:
            continue
        detector, subject, action = history.key.split(":", 2)
        keys.append({
            "key": history.key,
            "detector": detector,
            "subject": subject,
            "action": action,
            "recurrences": history.recurrences,
            "escalated": recurrence.should_escalate(history.recurrences),
            "next_cooldown_sec": recurrence.cooldown_for(history.recurrences, COOLDOWN_SEC),
            "cycles": [cycle.as_dict() for cycle in history.cycles],
        })
    keys.sort(key=lambda item: item["recurrences"], reverse=True)

    return {
        "ok": True,
        "window_sec": recurrence.WINDOW_SEC,
        "window_label": recurrence.window_label(),
        "limit": recurrence.LIMIT,
        "keys": keys,
    }


@app.post("/api/rca/sentinel/poll")
async def sentinel_poll(detector: str | None = None) -> dict[str, Any]:
    """Run one detect-decide-act cycle now, instead of waiting for the timer."""
    from .sentinel_wiring import poll_once

    try:
        result = await asyncio.to_thread(poll_once, detector)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {"ok": True, **result}


@app.get("/api/rca/cost/cache")
async def rca_cost_cache() -> dict[str, Any]:
    """What is cached on disk — i.e. what the next deploy will not have to re-buy."""
    from .model_access import cache_stats, calls_enabled

    stats = await asyncio.to_thread(cache_stats)
    return {
        "ok": True,
        "calls_enabled": calls_enabled(),
        "prewarm_enabled": os.getenv("AUTOPOIESIS_PREWARM", "0") == "1",
        **stats,
    }


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    if not settings.frontend_dist.exists():
        return PlainTextResponse(
            "Frontend not built. Run `npm run dev` (Vite) or `npm run build` in frontend/.",
            status_code=404,
        )
    dist_root = settings.frontend_dist.resolve()
    requested = (settings.frontend_dist / full_path).resolve()
    if full_path and requested.is_relative_to(dist_root) and requested.is_file():
        # Asset filenames carry a content hash, so they are safe to cache hard.
        return FileResponse(requested, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    # index.html must never be cached: it is the only file whose name does not
    # change between builds, so a cached copy keeps pointing at a bundle that
    # was replaced — which shows up as a deploy that silently did nothing.
    return FileResponse(
        dist_root / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )
