from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from .config import Settings
from .live import analyze_graph, assess_device, assess_mesh, assess_subnet, assess_wan, event_rate, subnet_graph
from .providers import list_providers
from .rca_reader import load_rca_snapshot, _load_topology

settings = Settings.from_env()
_CACHE_TTL_SEC = 5.0
_cache_lock = asyncio.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_loaded_at = 0.0


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
    _start_prewarm()
    yield


app = FastAPI(
    title="selfevo Network RCA Console",
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
def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
