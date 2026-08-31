"""Token accounting for every model call the system makes.

The provider used to discard the ``usage`` object the API returns, which meant
the only place spend existed was the vendor's dashboard — visible after the
fact, attributable to nothing. This records each call at the moment it happens,
tagged with what asked for it, so a bill can be traced back to a feature rather
than to a date.

On the money figure: the token counts are ground truth, reported by the API.
The **cost is computed from a local rate table and is an estimate** — rates
change, and cache-hit pricing is not visible in the usage object. The vendor
dashboard remains authoritative; this exists to answer "which feature spent
it, and is it accelerating" between dashboard refreshes.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

def _default_ledger() -> Path:
    """Where spend is recorded.

    Under pytest this moves to a temp path: a test run writing into the real
    ledger would put fabricated rows next to real spend, which is worse than
    having no ledger at all.
    """
    configured = os.getenv("AUTOPOIESIS_COST_LEDGER")
    if configured:
        return Path(configured)
    test_root = os.getenv("AUTOPOIESIS_TEST_TMP")
    if test_root:
        return Path(test_root) / "llm-cost.jsonl"
    # PYTEST_CURRENT_TEST is absent during collection, and `_` is the Python
    # executable under `python -m pytest`. They cannot protect a path captured
    # by an import. The conftest sets AUTOPOIESIS_TEST_TMP before collection;
    # this fallback covers direct imports performed later in a test body.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(tempfile.gettempdir()) / "autopoiesis-cost-test.jsonl"
    return Path("/data/autopoiesis-production/llm-cost.jsonl")


# Tests and embedders may inject a path explicitly. Leaving this unset avoids
# binding one early import to a production sink for the rest of the process.
LEDGER_PATH: Path | None = None


def _ledger_path() -> Path:
    return LEDGER_PATH if LEDGER_PATH is not None else _default_ledger()

# CNY per million tokens. Adjust to match the contract in force; the comment
# beside each entry says where the number came from so a stale rate is
# obvious rather than silently wrong.
RATES: dict[str, dict[str, float]] = {
    # Observed blended rate on 2026-08-22: ¥2.48 over 303,219 tokens ≈ ¥8.2/M.
    # Split here as a conservative estimate pending the published table.
    "deepseek-v4-pro": {"input": 4.0, "output": 16.0},
    "deepseek-chat": {"input": 1.0, "output": 2.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
}
FALLBACK_RATE = {"input": 4.0, "output": 16.0}

_LOCK = threading.Lock()


@dataclass
class Usage:
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    session_id: str = ""
    at: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_cny(self) -> float:
        rate = RATES.get(self.model, FALLBACK_RATE)
        return round(
            self.prompt_tokens / 1_000_000 * rate["input"]
            + self.completion_tokens / 1_000_000 * rate["output"],
            6,
        )

    def as_row(self) -> dict[str, Any]:
        return {
            "at": self.at or datetime.now(timezone.utc).isoformat(),
            "model": self.model,
            "purpose": self.purpose,
            "session_id": self.session_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_cny": self.cost_cny(),
        }


def record(
    model: str,
    purpose: str,
    usage: dict[str, Any] | None,
    session_id: str = "",
) -> Usage | None:
    """File one call's usage. A response without usage is recorded as zero.

    Returning None for a missing usage object would hide the call entirely; a
    zero-token row at least shows that something ran and that the provider did
    not say how much it cost.
    """
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    entry = Usage(
        model=model,
        purpose=purpose,
        prompt_tokens=prompt,
        completion_tokens=completion,
        session_id=session_id,
        at=datetime.now(timezone.utc).isoformat(),
    )
    row = entry.as_row()
    row["usage_reported"] = bool(usage)
    path = _ledger_path()
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        # Accounting must never take down the call it is accounting for.
        pass
    return entry


def _rows(since: datetime | None = None) -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    when = datetime.fromisoformat(str(row.get("at")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when < since:
                    continue
            out.append(row)
    return out


def summary(hours: int = 24) -> dict[str, Any]:
    """Spend over a window, broken down by what asked for it."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _rows(since)

    by_purpose: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "cost_cny": 0.0}
    )
    by_model: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "cost_cny": 0.0}
    )
    by_hour: dict[str, float] = defaultdict(float)

    for row in rows:
        for bucket, key in ((by_purpose, row.get("purpose", "?")), (by_model, row.get("model", "?"))):
            bucket[key]["calls"] += 1
            bucket[key]["tokens"] += int(row.get("total_tokens") or 0)
            bucket[key]["cost_cny"] = round(bucket[key]["cost_cny"] + float(row.get("cost_cny") or 0), 6)
        hour = str(row.get("at", ""))[:13]
        by_hour[hour] = round(by_hour[hour] + float(row.get("cost_cny") or 0), 6)

    total_cost = round(sum(float(row.get("cost_cny") or 0) for row in rows), 4)
    total_tokens = sum(int(row.get("total_tokens") or 0) for row in rows)
    largest = max(rows, key=lambda r: int(r.get("total_tokens") or 0), default=None)

    return {
        "window_hours": hours,
        "calls": len(rows),
        "total_tokens": total_tokens,
        "total_cost_cny": total_cost,
        "average_tokens_per_call": round(total_tokens / len(rows)) if rows else 0,
        "by_purpose": {k: dict(v) for k, v in sorted(by_purpose.items(), key=lambda kv: -kv[1]["cost_cny"])},
        "by_model": {k: dict(v) for k, v in by_model.items()},
        "by_hour": dict(sorted(by_hour.items())),
        "largest_call": largest,
        "recent": list(reversed(rows))[:30],
        "rates_note": "成本按本地费率表估算，权威数字以服务商账单为准；token 数来自 API 返回。",
    }
