"""Live signals: real-time event rate from R230, and on-demand DeepSeek threat analysis."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

# ---- real-time event rate (poll R230 syslog tail) ----
_rate_cache: dict[str, Any] = {"at": 0.0, "val": None}
_RATE_TTL = 4.0


def event_rate() -> dict[str, Any]:
    now = time.monotonic()
    if _rate_cache["val"] is not None and now - _rate_cache["at"] <= _RATE_TTL:
        return _rate_cache["val"]
    val = _probe_rate()
    _rate_cache.update(at=now, val=val)
    return val


def _probe_rate() -> dict[str, Any]:
    ssh = os.getenv("R230_SSH")
    pw = os.getenv("R230_PASS")
    log = os.getenv("R230_LOG", "/data/fortigate-runtime/input/fortigate.log")
    if not ssh or not pw:
        return {"eventsPerSec": None, "lines": 0, "live": False}
    # tail recent lines, read first+last timestamp, derive lines/sec
    # match the HH:MM:SS value of " time=" (not "eventtime=" epoch); first+last of the tail
    pat = "[^a-zA-Z]time=([0-9]{1,2}:[0-9]{2}:[0-9]{2})"
    remote = f"tail -n 6000 {log} | sed -nE '1s/.*{pat}.*/\\1/p; $s/.*{pat}.*/\\1/p'"
    try:
        out = subprocess.run(
            ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=6", ssh, remote],
            capture_output=True, text=True, timeout=12,
        ).stdout.strip().splitlines()
    except Exception:
        return {"eventsPerSec": None, "lines": 0, "live": False}
    times = [t for t in out if re.match(r"^\d+:\d+:\d+$", t)]
    if len(times) < 2:
        return {"eventsPerSec": None, "lines": 0, "live": False}

    def secs(t: str) -> int:
        h, m, s = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + s

    span = secs(times[-1]) - secs(times[0])
    if span <= 0:
        span = 1
    rate = round(6000 / span, 1)
    return {"eventsPerSec": rate, "lines": 6000, "spanSec": span, "live": True}


# ---- on-demand DeepSeek threat analysis for one device ----
def assess_device(ip: str, cidr: str, device: dict, lang: str = "zh") -> dict[str, Any]:
    from . import providers
    from core.llm.provider import OpenAICompatibleClient

    cfg = providers._deepseek_cfg()
    if not cfg["api_key"]:
        return {"ok": False, "text": "DeepSeek key not configured."}
    client = OpenAICompatibleClient(
        base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout_sec=40
    )
    want_lang = "Chinese" if lang == "zh" else "English"
    instr = (
        f"You are a network threat analyst. Assess this internal host from real FortiGate "
        f"firewall telemetry. Respond in {want_lang}, 2-3 sentences, affirmative and concrete: "
        f'what the host is most likely doing, the threat level, and one readonly next step. '
        f'Return JSON {{"verdict": <short label>, "severity": "high|medium|low", "analysis": <text>}}.'
    )
    payload = {
        "host": ip,
        "subnet": cidr,
        "denied_flows": device.get("deny"),
        "accepted_flows": device.get("accept"),
        "total_flows": device.get("flows"),
        "top_target_ports": device.get("top_ports"),
        "prior_threat_score": device.get("threat"),
    }
    try:
        out = client.complete_json(
            [{"role": "user", "content": instr + "\n" + json.dumps(payload)}],
            schema_name="threat_assessment",
        )
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "ip": ip,
        "verdict": out.get("verdict") or out.get("label") or "",
        "severity": out.get("severity", device.get("threat", "")),
        "analysis": out.get("analysis") or out.get("text") or "",
        "model": cfg["model"],
    }
