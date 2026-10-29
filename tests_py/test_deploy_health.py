"""Exercise rollout readiness against a real local HTTP server."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "frontend/script/wait_for_health.py"


@contextmanager
def endpoint(body=b'{"status":"ok"}', *, ready_after=0, delay=0):
    started = time.monotonic()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(delay)
            ready = time.monotonic() - started >= ready_after
            self.send_response(200 if ready else 503)
            self.end_headers()
            try:
                self.wfile.write(body if ready else b'{"status":"starting"}')
            except (BrokenPipeError, ConnectionResetError):
                pass  # The deadline test deliberately closes the client socket.

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/healthz", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def probe(url, timeout):
    return subprocess.run(
        [sys.executable, str(SCRIPT), url, "--timeout", str(timeout)],
        capture_output=True, text=True, timeout=6,
    )


def test_cold_start_retries_until_the_gateway_is_ready():
    with endpoint(ready_after=0.2) as (url, requests):
        result = probe(url, 3)
    assert result.returncode == 0, result.stderr
    assert len(requests) >= 2
    assert json.loads(result.stdout)["status"] == "ok"


@pytest.mark.parametrize("body,reason", [
    (b'{"status":"degraded"}', "degraded"),
    (b'{"runtime":{}}', "missing"),
    (b'not json', "invalid JSON"),
    (b'[]', "object"),
])
def test_http_success_cannot_hide_an_unhealthy_payload(body, reason):
    with endpoint(body) as (url, _requests):
        result = probe(url, 0.25)
    assert result.returncode == 1
    assert "deadline" in result.stderr
    assert reason in result.stderr


def test_unavailable_gateway_still_fails_at_the_deadline():
    with endpoint(ready_after=100) as (url, _requests):
        result = probe(url, 0.25)
    assert result.returncode == 1
    assert "503" in result.stderr


def test_a_slow_http_request_cannot_extend_the_total_budget():
    with endpoint(delay=2) as (url, _requests):
        started = time.monotonic()
        result = probe(url, 0.3)
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < 1.5
    assert "deadline" in result.stderr


@pytest.mark.parametrize("timeout", [0, -1, "nan", "inf"])
def test_invalid_deadlines_are_rejected_before_network_access(timeout):
    result = probe("http://127.0.0.1:1/api/healthz", timeout)
    assert result.returncode == 2
    assert "finite positive" in result.stderr
