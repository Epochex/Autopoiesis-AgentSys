"""Shared host and HTTP helpers for the two controlled defence demos."""
from __future__ import annotations

import json
import argparse
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "http://127.0.0.1:8026"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, text=True, capture_output=True)


def wait_unit_state(unit: str, expected: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    last = "unknown"
    while time.monotonic() < deadline:
        result = run_command(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            check=False,
        )
        last = result.stdout.strip() or result.stderr.strip() or "unknown"
        if last == expected:
            return
        time.sleep(0.2)
    raise RuntimeError(f"{unit} did not reach {expected}; current state is {last}")


def install_demo_unit(unit: str, body: str) -> Path:
    if not unit.startswith("autopoiesis-acceptance-") or not unit.endswith(".service"):
        raise ValueError("controlled unit must use the autopoiesis-acceptance-*.service prefix")
    path = Path("/run/systemd/system") / unit
    if run_command(['systemctl', 'show', unit, '-p', 'LoadState', '--value'], check=False).stdout.strip() != 'not-found':
        raise RuntimeError(f'Refusing to replace existing service: {unit}')
    with path.open('x', encoding='utf-8') as handle:
        handle.write(body.rstrip() + '\n')
    try:
        run_command(["systemctl", "daemon-reload"])
    except BaseException:
        path.unlink(missing_ok=True)
        run_command(["systemctl", "daemon-reload"], check=False)
        raise
    return path


def remove_demo_unit(unit: str, path: Path, marker: Path | None = None) -> None:
    run_command(["systemctl", "stop", unit], check=False)
    run_command(["systemctl", "reset-failed", unit], check=False)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if marker is not None:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    run_command(["systemctl", "daemon-reload"], check=False)


def http_json(
    method: str,
    route: str,
    payload: Mapping[str, Any] | None = None,
    *,
    query: Mapping[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    url = f"{GATEWAY}{route}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    encoded = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"gateway is unavailable at {GATEWAY}: {error.reason}") from error
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError(f"gateway returned an invalid result: {result!r}")
    return result


def print_receipt(record: Mapping[str, Any], next_page: str) -> None:
    print(json.dumps(dict(record), ensure_ascii=False, indent=2))
    print(f"\n演示已完成。打开 {GATEWAY}{next_page} 查看结果。")


def demo_options(description: str):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--test', action='store_true', help='缩短观看等待，执行相同真实验收')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('需要 root 权限，仅创建和操作 autopoiesis-acceptance-* 测试服务')
    http_json('GET', '/api/healthz')
    return args


def stage(message: str, seconds: float = 0):
    print(message, flush=True)
    if seconds:
        time.sleep(seconds)
