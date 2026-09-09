"""Deterministic natural-language boundary for investigation source planning."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_UNIT = re.compile(r"(?<![\w.-])([A-Za-z0-9_.@-]+\.(?:service|target|socket|timer))(?![\w.-])")
_DURATION_ZH = re.compile(r"(?:过去|最近)\s*(\d+)\s*(分钟|小时|天)")
_DURATION_EN = re.compile(r"\b(?:last|past)\s+(\d+)\s*(minutes?|mins?|hours?|days?)\b", re.I)


@dataclass(frozen=True, slots=True)
class InvestigationQueryContract:
    subject: str | None
    intent: str
    sources: tuple[str, ...]
    window_start: str | None
    window_end: str | None
    bindings: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "intent": self.intent,
            "sources": list(self.sources),
            "windowStart": self.window_start,
            "windowEnd": self.window_end,
            "bindings": dict(self.bindings),
        }


def _valid_ip_candidates(text: str) -> list[str]:
    values: list[str] = []
    for match in _IPV4.findall(text):
        try:
            value = str(ipaddress.ip_address(match))
        except ValueError:
            continue
        if value not in values:
            values.append(value)
    return values


def _duration(text: str) -> timedelta | None:
    match = _DURATION_ZH.search(text)
    if match:
        value, unit = int(match.group(1)), match.group(2)
        return {
            "分钟": timedelta(minutes=value),
            "小时": timedelta(hours=value),
            "天": timedelta(days=value),
        }[unit]
    match = _DURATION_EN.search(text)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    if unit.startswith(("minute", "min")):
        return timedelta(minutes=value)
    if unit.startswith("hour"):
        return timedelta(hours=value)
    return timedelta(days=value)


def parse_investigation_query(
    question: str,
    *,
    subject: str | None = None,
    now: datetime | None = None,
) -> InvestigationQueryContract:
    """Bind exact entities/time and choose data sources without an LLM call."""
    text = question.strip()
    lower = text.casefold()
    bindings: list[tuple[str, str]] = []
    resolved_subject = subject.strip() if subject and subject.strip() else None
    if resolved_subject:
        bindings.append(("subject", "explicit"))
    else:
        ips = _valid_ip_candidates(text)
        unit = _UNIT.search(text)
        if ips:
            resolved_subject = ips[0]
            bindings.append(("subject", "ipv4_literal"))
        elif unit:
            resolved_subject = unit.group(1)
            bindings.append(("subject", "systemd_unit_literal"))

    root_words = (
        "根因", "原因", "为什么", "故障", "异常", "失败", "断开", "不可用",
        "rca", "root cause", "failed", "failure", " down",
    )
    history_words = ("日志", "历史", "趋势", "发生过", "过去", "最近", "log", "history", "trend")
    current_words = ("当前", "现在", "实时", "状态", "在线", "current", "now", "status", "online")
    howto_words = ("怎么配置", "如何配置", "命令", "手册", "how to", "configure", "manual")
    has_root = any(word in lower for word in root_words)
    has_history = any(word in lower for word in history_words)
    has_current = any(word in lower for word in current_words)
    has_howto = any(word in lower for word in howto_words)

    if has_root:
        intent = "root_cause"
        sources = ("direct_state", "event_window", "operational_memory", "official_knowledge")
    elif has_howto:
        intent = "procedure_lookup"
        sources = ("official_knowledge", "operational_memory")
    elif has_history and not has_current:
        intent = "event_history"
        sources = ("event_window",)
    elif has_current and resolved_subject:
        intent = "current_status"
        sources = ("direct_state",)
    else:
        intent = "triage"
        sources = ("direct_state", "event_window", "operational_memory", "official_knowledge")

    duration = _duration(text)
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - duration if duration else None
    if duration:
        bindings.append(("timeWindow", "duration_literal"))
    return InvestigationQueryContract(
        subject=resolved_subject,
        intent=intent,
        sources=sources,
        window_start=start.isoformat() if start else None,
        window_end=end.isoformat() if start else None,
        bindings=tuple(bindings),
    )


__all__ = ["InvestigationQueryContract", "parse_investigation_query"]
