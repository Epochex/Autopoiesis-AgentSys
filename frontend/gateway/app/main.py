from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from .config import Settings
from .live import assess_device, assess_subnet, event_rate
from .providers import list_providers
from .rca_reader import load_rca_snapshot, _load_topology

settings = Settings.from_env()
_CACHE_TTL_SEC = 5.0
_cache_lock = asyncio.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_loaded_at = 0.0

app = FastAPI(
    title="selfevo Network RCA Console",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

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
    if device is None:
        return {"ok": False, "text": "device not found"}
    return await asyncio.to_thread(assess_device, ip, cidr, device, lang, peers)


@app.get("/api/rca/threat_subnet")
async def rca_threat_subnet(cidr: str, lang: str = "zh") -> dict[str, Any]:
    return await asyncio.to_thread(assess_subnet, cidr, lang)


@app.get("/api/rca/snapshot")
async def rca_snapshot(provider: str = "rule", refresh: bool = False) -> dict[str, Any]:
    return await _get_snapshot(provider=provider, force=refresh)


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
