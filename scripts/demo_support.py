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


def create_demo_case(
    repository: Any,
    *,
    run_id: str,
    scenario: str,
    subject: str,
    summary: str,
    source_suffix: str = "incident",
    extra_payload: Mapping[str, Any] | None = None,
) -> str:
    """Create one real, queryable case for a controlled demonstration."""
    from domains.network_rca.investigation_case import CaseObservation, SourceReference

    observed_at = utc_now()
    payload = {
        "dataClassification": "controlled_test",
        "demoRunId": run_id,
        "demoScenario": scenario,
        "incidentFacts": {
            "dataClassification": "controlled_test",
            "observedAt": observed_at,
            "detector": "failed_units",
            "faultClass": "service_failed",
            "subjectKind": "systemd_unit",
            "managedDevice": subject,
        },
        **dict(extra_payload or {}),
    }
    case = repository.ingest(CaseObservation(
        source=SourceReference("controlled_demo", f"{run_id}:{source_suffix}"),
        occurred_at=observed_at,
        severity="high",
        subject=subject,
        service="service_health",
        rule_id="failed_units",
        scope="controlled_demo",
        summary=summary,
        payload=payload,
    ))
    return case.case_id


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
    metrics = dict(record.get("metrics") or {})
    en = LANG == "en"
    summary = {
        ("result" if en else "结果"): record.get("status"),
        ("scenario" if en else "场景"): record.get("scenario"),
        ("case" if en else "案件"): record.get("caseId"),
        ("subject" if en else "对象"): record.get("subject"),
    }
    if record.get("scenario") == "self_healing":
        summary["readback" if en else "回读"] = (
            f"{metrics.get('readbackSamples', 0)} samples; target and guard checks all passed" if en
            else f"{metrics.get('readbackSamples', 0)} 次，目标与保护检查均通过")
    elif record.get("scenario") == "memory_trajectory":
        summary["memory benefit" if en else "记忆收益"] = (
            f"checks {metrics.get('baselineExecutedProbes', 0)}→{metrics.get('executedProbes', 0)}, "
            f"{metrics.get('actualSavedProbes', 0)} avoided" if en
            else f"检查 {metrics.get('baselineExecutedProbes', 0)}→{metrics.get('executedProbes', 0)}，"
                 f"减少 {metrics.get('actualSavedProbes', 0)} 项"
        )
        summary["request time" if en else "本次耗时"] = (
            f"{float(metrics.get('baselineRequestSeconds', 0)):.1f}s→"
            f"{float(metrics.get('memoryRequestSeconds', 0)):.1f}s"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if en:
        print(f"\nDemonstration complete. The result is in the incident centre: {GATEWAY}{next_page}")
    else:
        print(f"\n演示已完成。结果已留在事件中心：{GATEWAY}{next_page}")


LANG = os.getenv("AUTOPOIESIS_DEMO_LANG", "en").strip().lower() or "en"


def demo_options(description: str):
    global LANG
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--test', action='store_true', help='缩短观看等待，执行相同真实验收 / shorten the pauses, same real acceptance')
    parser.add_argument('--lang', choices=('en', 'zh'), default=LANG,
                        help='terminal narration language (default en; AUTOPOIESIS_DEMO_LANG overrides)')
    args = parser.parse_args()
    LANG = args.lang
    if os.geteuid() != 0:
        parser.error('需要 root 权限，仅创建和操作 autopoiesis-acceptance-* 测试服务 / root is required; only autopoiesis-acceptance-* test units are created or touched')
    http_json('GET', '/api/healthz')
    return args


def stage(message: str, seconds: float = 0, en: str | None = None):
    """A headline step. `message` is Chinese; `en` is the English the terminal
    prints when narration is English. Either language alone is complete."""
    print(en if (LANG == "en" and en) else message, flush=True)
    if seconds:
        time.sleep(seconds)


def say(en: str, zh: str | None = None) -> None:
    """A detail line under the current step: what was actually done, on what,
    with which command. The terminal is the audit trail the audience reads."""
    text = en if (LANG == "en" or not zh) else zh
    for line in text.splitlines():
        print("   " + line if line else "", flush=True)
