from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings
from .live import analyze_graph, assess_device, assess_mesh, assess_subnet, assess_wan, event_rate, subnet_graph
from .providers import list_providers
from .rca_reader import load_rca_snapshot, _load_topology

settings = Settings.from_env()
_CACHE_TTL_SEC = 5.0
_cache_lock = asyncio.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_loaded_at = 0.0
_evolving_service = None
_runtime_error: str | None = None
_diagnosis_cases: dict[str, Any] = {}


class RCADiagnosisRequest(BaseModel):
    """Select a server-validated case without accepting client-authored skills."""

    case_id: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)


def _start_prewarm() -> None:
    """Warm the DeepSeek subnet models in a daemon thread so the UI gets instant cached hits."""
    import threading

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
    global _diagnosis_cases, _evolving_service, _runtime_error
    _start_prewarm()
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
                raise_on_evolution_error=False,
            )
            _runtime_error = None
        else:
            _runtime_error = "validated RCA dataset is unavailable"
    except Exception as exc:
        _runtime_error = f"{type(exc).__name__}: {exc}"
    try:
        yield
    finally:
        if _evolving_service is not None:
            await asyncio.to_thread(_evolving_service.close)
        _diagnosis_cases = {}


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
    return {
        "status": "ok" if runtime.get("last_error") is None else "degraded",
        "durableMemory": _evolving_service.memory.repository is not None,
        "runtime": runtime,
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
    return await _get_snapshot(provider=provider, force=refresh)


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


@app.get("/api/rca/evolution")
async def rca_evolution(passes: int = Query(default=4, ge=1, le=64)) -> dict[str, Any]:
    from .rca_reader import load_evolution
    return await asyncio.to_thread(load_evolution, None, passes)


@app.get("/api/rca/pentest")
async def rca_pentest(lang: str = "zh") -> dict[str, Any]:
    # Read-only self-pentest report built from the active_recon mock surface.
    # Intrusive weak-cred/exploit probes are approval-gated and never executed.
    from domains.active_recon.pentest import build_pentest_report
    return await asyncio.to_thread(build_pentest_report, lang)


@app.get("/api/rca/live-situation")
async def rca_live_situation() -> dict[str, Any]:
    # Read-only tail of the NetOps real-time subsystem's landed sink files (alerts +
    # AIOps suggestions + cluster-state). The gateway never joins the Redpanda topic;
    # the two subsystems meet only at this disk boundary. Returns empty collections
    # when the runtime dir is absent, so the panel degrades to "no live data".
    from .runtime_reader import load_runtime_snapshot
    return await asyncio.to_thread(load_runtime_snapshot, settings)


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
        return FileResponse(requested)
    return FileResponse(dist_root / "index.html")
