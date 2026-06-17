from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from .config import Settings
from .control_loop_reader import build_control_loop_stream_delta, load_control_loop_snapshot

settings = Settings.from_env()
SNAPSHOT_CACHE_TTL_SEC = max(2.0, settings.stream_interval_sec * 2.5)
_snapshot_cache_lock = asyncio.Lock()
_snapshot_cache_payload: dict[str, Any] | None = None
_snapshot_cache_loaded_at = 0.0

app = FastAPI(
  title='ContextHelix Console Gateway',
  version='0.1.0',
  docs_url='/api/docs',
  openapi_url='/api/openapi.json',
)

if settings.cors_origins:
  allow_all = '*' in settings.cors_origins
  app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'] if allow_all else list(settings.cors_origins),
    allow_credentials=not allow_all,
    allow_methods=['*'],
    allow_headers=['*'],
  )


@app.get('/api/healthz')
def healthz() -> dict[str, str]:
  return {'status': 'ok'}


async def get_control_loop_snapshot(force_refresh: bool = False) -> dict[str, Any]:
  global _snapshot_cache_loaded_at, _snapshot_cache_payload

  now = time.monotonic()
  if (
    not force_refresh
    and _snapshot_cache_payload is not None
    and now - _snapshot_cache_loaded_at <= SNAPSHOT_CACHE_TTL_SEC
  ):
    return _snapshot_cache_payload

  async with _snapshot_cache_lock:
    now = time.monotonic()
    if (
      not force_refresh
      and _snapshot_cache_payload is not None
      and now - _snapshot_cache_loaded_at <= SNAPSHOT_CACHE_TTL_SEC
    ):
      return _snapshot_cache_payload

    payload = await asyncio.to_thread(load_control_loop_snapshot, settings)
    _snapshot_cache_payload = payload
    _snapshot_cache_loaded_at = time.monotonic()
    return payload


@app.on_event('startup')
async def warm_control_loop_snapshot_cache():
  return None


@app.get('/api/control-loop/snapshot')
async def control_loop_snapshot():
  return await get_control_loop_snapshot()


@app.get('/api/control-loop/stream')
async def control_loop_stream(request: Request):
  async def event_stream():
    previous_payload: dict[str, object] | None = None
    idle_cycles = 0

    # Emit an immediate heartbeat so browser clients clear their handshake guard
    # even if the first live snapshot takes longer than expected to assemble.
    yield (
      'retry: 2000\n'
      f"event: heartbeat\ndata: {json.dumps({'type': 'heartbeat', 'emittedAt': 'connecting'}, ensure_ascii=False)}\n\n"
    )

    while True:
      if await request.is_disconnected():
        break

      payload = await get_control_loop_snapshot()
      if previous_payload is None:
        envelope = {
          'type': 'snapshot',
          'emittedAt': payload['controlLoop']['latestSuggestionTs'],
          'snapshot': payload,
        }
        yield (
          'retry: 2000\n'
          f"event: snapshot\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
        )
        previous_payload = payload
        idle_cycles = 0
        await asyncio.sleep(settings.stream_interval_sec)
        continue

      delta = await asyncio.to_thread(
        build_control_loop_stream_delta,
        previous_payload,
        payload,
      )
      if delta:
        envelope = {
          'type': 'delta',
          'emittedAt': delta['emittedAt'],
          'snapshot': payload,
          'delta': delta,
        }
        yield (
          'retry: 2000\n'
          f"event: delta\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
        )
        previous_payload = payload
        idle_cycles = 0
      else:
        idle_cycles += 1
        if idle_cycles * settings.stream_interval_sec >= 15:
          heartbeat = {
            'type': 'heartbeat',
            'emittedAt': payload['controlLoop']['latestSuggestionTs'],
          }
          yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
          idle_cycles = 0

      await asyncio.sleep(settings.stream_interval_sec)

  return StreamingResponse(
    event_stream(),
    media_type='text/event-stream',
    headers={
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  )


@app.get('/', include_in_schema=False)
@app.get('/{full_path:path}', include_in_schema=False)
def serve_frontend(full_path: str = ''):
  if not settings.frontend_dist.exists():
    return PlainTextResponse(
      'Frontend dist not built yet. Run Vite on :5173 for development or build frontend/ before serving from FastAPI.',
      status_code=404,
    )

  requested_path = (settings.frontend_dist / full_path).resolve()
  dist_root = settings.frontend_dist.resolve()
  if (
    full_path
    and requested_path.is_relative_to(dist_root)
    and requested_path.is_file()
  ):
    return FileResponse(requested_path)
  return FileResponse(dist_root / 'index.html')
