"""Pluggable incident-source abstraction for the network-RCA domain.

A demo (or any caller) can switch where incident evidence comes from WITHOUT
the framework ever reaching out to a live device on its own:

    from domains.network_rca.incident_source import select_source

    for event in select_source():            # "replay" is the safe default
        ...                                   # committed/replayed fixture syslog

    for event in select_source("live"):       # ONLY works if a human opted in
        ...                                   # raises otherwise -- see LiveSource

Design guarantees
-----------------
* ReplaySource is the DEFAULT. It reads committed/replayed FortiOS syslog
  files (or an explicitly injected batch of lines) and opens no connection.
* LiveSource is CONFIG-GATED and disabled by default. It only ever reads a
  local file path that a human has explicitly handed to it via the
  ``AUTOPOIESIS_LIVE_SYSLOG_PATH`` environment variable (mirroring the
  feature-flag pattern already used by ``adapters.live_device`` and the
  read-only ingestor client). It never dials, hardcodes, or discovers any host
  or IP -- the "live" feed is whatever file a human points that variable at
  (e.g. a path that a log collector is writing). If the variable is unset it
  is disabled and raises a clear error.

Event shape
-----------
Every source yields plain dicts that are byte-for-byte identical to
``adapters.fortios_syslog.FortiOSLogEvent(...).model_dump(mode="json")`` -- the
same records the read-only ingestor app serves over ``/logs`` and the same
key/value model ``adapters.real_syslog_adapter`` aggregates over. Emitting that
canonical shape is what lets the replay and live sources be swapped freely.
Keys: see ``EVENT_FIELDS``.
"""
from __future__ import annotations

import gzip
import itertools
import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.env import autopoiesis_env

# Canonical event keys -- kept identical to FortiOSLogEvent.model_dump() so the
# replay and live sources emit output compatible with the rest of the domain.
EVENT_FIELDS: tuple[str, ...] = (
    "timestamp",
    "type",
    "subtype",
    "level",
    "logid",
    "srcip",
    "dstip",
    "action",
    "policyid",
    "msg",
    "raw",
    "fields",
)

# Default replay corpus: the committed/replayed FortiOS syslog fixtures.
DEFAULT_SYSLOG_DIR: Path = Path(__file__).resolve().parent / "fixtures" / "real" / "syslog"

# The single, human-set switch that enables the live source. Nothing else can
# turn it on -- there is no default value and no discovery.
LIVE_SYSLOG_ENV_VAR: str = "AUTOPOIESIS_LIVE_SYSLOG_PATH"

# Exact operator-facing message when the live source is not opted into.
LIVE_DISABLED_MESSAGE: str = (
    "live source not enabled -- set AUTOPOIESIS_LIVE_SYSLOG_PATH to a local syslog "
    "file that a human has explicitly pointed at the live feed"
)


def parse_syslog_line(line: str) -> dict[str, Any]:
    """Parse one FortiOS key=value syslog line into the canonical event dict.

    Faithfully reproduces ``adapters.fortios_syslog.parse_fortios_kv_line`` so
    the output equals ``FortiOSLogEvent(...).model_dump(mode="json")``. Kept
    self-contained on purpose: this module pulls in no client/transport code,
    so the default path cannot import a network stack at all.
    """
    fields: dict[str, str] = {}
    for token in shlex.split(line.strip()):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value.strip('"')

    timestamp: str | None = None
    if fields.get("date") and fields.get("time"):
        timestamp = f"{fields['date']}T{fields['time']}"

    return {
        "timestamp": timestamp,
        "type": fields.get("type"),
        "subtype": fields.get("subtype"),
        "level": fields.get("level"),
        "logid": fields.get("logid"),
        "srcip": fields.get("srcip"),
        "dstip": fields.get("dstip"),
        "action": fields.get("action"),
        "policyid": fields.get("policyid"),
        "msg": fields.get("msg"),
        "raw": line.rstrip("\n"),
        "fields": fields,
    }


def default_replay_paths() -> list[Path]:
    """Sorted list of committed/replayed syslog fixtures (deterministic order).

    Returns an empty list if the corpus is absent (the ``fixtures/real`` tree
    is git-ignored, so a fresh checkout may not have it); callers should fall
    back to an injected batch in that case.
    """
    if not DEFAULT_SYSLOG_DIR.is_dir():
        return []
    return sorted(DEFAULT_SYSLOG_DIR.glob("*.log"))


def _iter_file_lines(path: Path) -> Iterator[str]:
    """Lazily yield lines from a plain or ``.gz`` local file (no eager read)."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                yield line
    else:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                yield line


class IncidentSource(ABC):
    """One interface over every place incidents can come from.

    Subclasses only supply ``_raw_lines``; parsing, blank-line skipping and the
    optional ``limit`` are shared here so replay and live stay identical apart
    from *where the bytes originate*.
    """

    #: short provenance label, e.g. "replay" or "live".
    name: str = "incident-source"
    _limit: int | None = None

    @abstractmethod
    def _raw_lines(self) -> Iterator[str]:
        """Yield raw syslog lines. Must not open any live connection."""

    def events(self) -> Iterator[dict[str, Any]]:
        """Yield canonical event dicts (see ``EVENT_FIELDS``) lazily."""
        count = 0
        for line in self._raw_lines():
            if not line.strip():
                continue
            yield parse_syslog_line(line)
            count += 1
            if self._limit is not None and count >= self._limit:
                return

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.events()

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Materialize events into a list, optionally capping at ``limit``."""
        if limit is None:
            return list(self.events())
        return list(itertools.islice(self.events(), limit))

    def describe(self) -> dict[str, Any]:
        """Provenance for logs/UI -- what this source is and where it reads."""
        return {"name": self.name, "gated": False, "limit": self._limit}


class ReplaySource(IncidentSource):
    """Safe default: replay committed fixture syslog or an injected batch.

    * ``ReplaySource()`` reads the committed/replayed fixtures under
      ``fixtures/real/syslog/*.log`` in a deterministic (sorted) order.
    * ``ReplaySource(lines=[...])`` replays an explicitly injected batch and
      touches no filesystem at all -- handy for demos and tests.
    * ``ReplaySource(paths=[...])`` replays specific files.

    No path here is ever a host or IP; iteration performs local file reads only.
    """

    name = "replay"

    def __init__(
        self,
        paths: Iterable[str | Path] | None = None,
        *,
        lines: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> None:
        self._lines: list[str] | None = list(lines) if lines is not None else None
        if paths is not None:
            self._paths: list[Path] = [Path(p) for p in paths]
        else:
            self._paths = default_replay_paths()
        self._limit = limit

    def _raw_lines(self) -> Iterator[str]:
        if self._lines is not None:
            yield from self._lines
            return
        for path in self._paths:
            if path.exists():
                yield from _iter_file_lines(path)

    def describe(self) -> dict[str, Any]:
        origin = "injected-batch" if self._lines is not None else [str(p) for p in self._paths]
        return {"name": self.name, "gated": False, "origin": origin, "limit": self._limit}


class LiveSource(IncidentSource):
    """Config-gated live source. Disabled by default; never auto-connects.

    Enabling it is a deliberate human action: set ``AUTOPOIESIS_LIVE_SYSLOG_PATH``
    to a local file that a collector is writing from the live feed (or pass an
    explicit ``path``). With neither provided the source refuses to run and
    raises ``RuntimeError(LIVE_DISABLED_MESSAGE)``. It reads a file path only --
    it does not, and cannot, dial or hardcode any host or IP.
    """

    name = "live"

    def __init__(self, path: str | Path | None = None, *, limit: int | None = None) -> None:
        resolved = path if path is not None else autopoiesis_env("LIVE_SYSLOG_PATH")
        if not resolved:
            raise RuntimeError(LIVE_DISABLED_MESSAGE)
        self._path = Path(resolved)
        self._limit = limit

    def _raw_lines(self) -> Iterator[str]:
        # Pure local file read of the human-configured path. No connection.
        yield from _iter_file_lines(self._path)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gated": True,
            "env_var": LIVE_SYSLOG_ENV_VAR,
            "origin": str(self._path),
            "limit": self._limit,
        }


def select_source(mode: str = "replay", **kwargs: Any) -> IncidentSource:
    """Factory: return the incident source for ``mode`` (default ``"replay"``).

    * ``"replay"`` (default) -> :class:`ReplaySource`, the safe demo source.
    * ``"live"`` -> :class:`LiveSource`, which raises unless a human has set
      ``AUTOPOIESIS_LIVE_SYSLOG_PATH`` (or passed an explicit ``path=``).

    Mode is always explicit -- it is never read from the environment, so the
    live source can never be turned on implicitly. Extra keyword arguments are
    forwarded to the chosen source (e.g. ``limit=``, ``lines=``, ``path=``).
    """
    normalized = (mode or "replay").strip().lower()
    if normalized == "replay":
        return ReplaySource(**kwargs)
    if normalized == "live":
        return LiveSource(**kwargs)
    raise ValueError(f"unknown incident source mode: {mode!r} (expected 'replay' or 'live')")
