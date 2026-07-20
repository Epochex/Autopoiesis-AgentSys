"""Falsifiable tests for the pluggable incident-source abstraction.

These are deterministic and committable: the strict assertions run against an
INJECTED BATCH of synthetic-but-real-shaped FortiOS syslog lines (no company
data, no live host), mirroring the philosophy of ``test_real_rca.py``. The
committed/replayed ``fixtures/real/syslog`` corpus is git-ignored, so the tests
that touch it are guarded with ``skipif`` and never required.

The safety-critical claims this file pins down:
  * ``select_source()`` defaults to the safe replay source.
  * The replay source yields parseable events deterministically.
  * The live source is disabled by default and raises a clear, actionable error.
  * The default (replay) code path opens NO socket / dials NO host, and the
    module itself imports no network stack and hardcodes no host or IP.
"""
from __future__ import annotations

import itertools
import socket
from pathlib import Path

import pytest

from domains.network_rca import incident_source as isrc
from domains.network_rca.incident_source import (
    EVENT_FIELDS,
    LIVE_DISABLED_MESSAGE,
    LIVE_SYSLOG_ENV_VAR,
    IncidentSource,
    LiveSource,
    ReplaySource,
    default_replay_paths,
    parse_syslog_line,
    select_source,
)

# Synthetic FortiOS-shaped lines (real key/value grammar, invented values).
# Covers an admin-auth event, a DHCP event, and a denied traffic flow.
SAMPLE_LINES = [
    'date=2026-04-08 time=00:01:06 type="event" subtype="system" level="alert" '
    'logid="0100032002" srcip=203.0.113.7 dstip=198.51.100.1 action="login" '
    'status="failed" msg="Administrator login failed"',
    'date=2026-06-16 time=00:01:31 type="event" subtype="system" level="information" '
    'logid="0100026001" action="perf-stats" msg="DHCP server sends a DHCPACK"',
    'date=2026-06-16 time=00:04:16 type="traffic" subtype="forward" level="notice" '
    'logid="0000000013" srcip=192.0.2.55 dstip=198.51.100.9 action="deny" policyid=21 '
    'msg="denied flow"',
]

FIXTURES_PRESENT = bool([p for p in default_replay_paths() if p.exists()])
_skip_no_fixtures = pytest.mark.skipif(
    not FIXTURES_PRESENT, reason="git-ignored fixtures/real/syslog corpus is absent"
)


@pytest.fixture(autouse=True)
def _live_env_disabled(monkeypatch):
    """Guarantee the live source is OFF unless a test explicitly opts in."""
    monkeypatch.delenv(LIVE_SYSLOG_ENV_VAR, raising=False)
    monkeypatch.delenv("SELFEVO_LIVE_SYSLOG_PATH", raising=False)


# --------------------------------------------------------------------------- #
# Parser / event shape                                                        #
# --------------------------------------------------------------------------- #
def test_parse_yields_canonical_event_shape():
    event = parse_syslog_line(SAMPLE_LINES[0])
    assert tuple(event.keys()) == EVENT_FIELDS
    assert event["type"] == "event"
    assert event["subtype"] == "system"
    assert event["logid"] == "0100032002"
    assert event["action"] == "login"
    assert event["timestamp"] == "2026-04-08T00:01:06"
    assert event["fields"]["status"] == "failed"
    assert event["raw"] == SAMPLE_LINES[0]


def test_events_are_compatible_with_fortios_log_event():
    """Round-trip proves output equals the domain's canonical event model."""
    from domains.network_rca.adapters.fortios_syslog import (
        FortiOSLogEvent,
        parse_fortios_kv_line,
    )

    for line in SAMPLE_LINES:
        emitted = parse_syslog_line(line)
        reference = parse_fortios_kv_line(line).model_dump(mode="json")
        assert emitted == reference
        # And the emitted dict re-hydrates into the model without loss.
        assert FortiOSLogEvent(**emitted).model_dump(mode="json") == emitted


# --------------------------------------------------------------------------- #
# ReplaySource: parseable, deterministic, injectable                          #
# --------------------------------------------------------------------------- #
def test_replay_source_yields_parseable_events_from_injected_batch():
    src = ReplaySource(lines=SAMPLE_LINES)
    assert isinstance(src, IncidentSource)
    events = list(src)
    assert len(events) == len(SAMPLE_LINES)
    assert all(tuple(e.keys()) == EVENT_FIELDS for e in events)
    assert [e["action"] for e in events] == ["login", "perf-stats", "deny"]
    assert events[2]["srcip"] == "192.0.2.55"
    assert events[2]["policyid"] == "21"


def test_replay_source_skips_blank_lines():
    src = ReplaySource(lines=["", "   ", SAMPLE_LINES[0], "\n"])
    assert len(list(src)) == 1


def test_replay_source_is_deterministic():
    first = list(ReplaySource(lines=SAMPLE_LINES))
    second = list(ReplaySource(lines=SAMPLE_LINES))
    assert first == second
    # Re-iterating a fresh source reproduces the exact same ordering/content.
    assert [e["logid"] for e in first] == [e["logid"] for e in second]


def test_replay_source_respects_limit():
    assert len(ReplaySource(lines=SAMPLE_LINES, limit=2).read()) == 2
    assert len(ReplaySource(lines=SAMPLE_LINES).read(limit=1)) == 1


def test_replay_describe_reports_injected_batch():
    d = ReplaySource(lines=SAMPLE_LINES).describe()
    assert d["name"] == "replay"
    assert d["gated"] is False
    assert d["origin"] == "injected-batch"


@_skip_no_fixtures
def test_replay_default_reads_committed_fixture_when_present():
    src = select_source()
    assert isinstance(src, ReplaySource)
    events = list(itertools.islice(src.events(), 25))
    assert len(events) == 25
    assert all(tuple(e.keys()) == EVENT_FIELDS for e in events)
    assert all(e["raw"] for e in events)
    # Deterministic across independent iterations of the real corpus, too.
    again = list(itertools.islice(select_source().events(), 25))
    assert [e["raw"] for e in events] == [e["raw"] for e in again]


# --------------------------------------------------------------------------- #
# Factory defaults to the safe source                                         #
# --------------------------------------------------------------------------- #
def test_factory_defaults_to_replay():
    assert isinstance(select_source(), ReplaySource)
    assert select_source().name == "replay"
    assert isinstance(select_source("replay"), ReplaySource)
    assert isinstance(select_source(mode="replay"), ReplaySource)


def test_factory_rejects_unknown_mode():
    with pytest.raises(ValueError):
        select_source("carrier-pigeon")


def test_factory_does_not_read_mode_from_environment(monkeypatch):
    """A stray env var must never silently flip the default to live."""
    monkeypatch.setenv("AUTOPOIESIS_INCIDENT_SOURCE", "live")
    monkeypatch.setenv("SELFEVO_INCIDENT_SOURCE", "live")
    monkeypatch.setenv("INCIDENT_SOURCE", "live")
    assert isinstance(select_source(), ReplaySource)


# --------------------------------------------------------------------------- #
# LiveSource: config-gated, disabled by default, no hardcoded host            #
# --------------------------------------------------------------------------- #
def test_live_source_disabled_by_default():
    with pytest.raises(RuntimeError) as excinfo:
        LiveSource()
    message = str(excinfo.value)
    assert "live source not enabled" in message
    assert "AUTOPOIESIS_LIVE_SYSLOG_PATH" in message
    assert message == LIVE_DISABLED_MESSAGE


def test_live_source_via_factory_is_also_gated():
    with pytest.raises(RuntimeError) as excinfo:
        select_source("live")
    assert "AUTOPOIESIS_LIVE_SYSLOG_PATH" in str(excinfo.value)


def test_live_source_reads_only_the_human_set_path(monkeypatch, tmp_path):
    """When a human opts in, live reads THAT local file -- not any host/IP."""
    feed = tmp_path / "live_feed.log"
    feed.write_text("\n".join(SAMPLE_LINES) + "\n", encoding="utf-8")
    monkeypatch.setenv(LIVE_SYSLOG_ENV_VAR, str(feed))

    src = select_source("live")
    assert isinstance(src, LiveSource)
    assert src.name == "live"
    events = list(src)
    assert [e["action"] for e in events] == ["login", "perf-stats", "deny"]

    described = src.describe()
    assert described["gated"] is True
    assert described["env_var"] == LIVE_SYSLOG_ENV_VAR
    assert described["origin"] == str(feed)


def test_live_source_accepts_legacy_env_as_read_only_fallback(monkeypatch, tmp_path):
    feed = tmp_path / "legacy_live_feed.log"
    feed.write_text(SAMPLE_LINES[0] + "\n", encoding="utf-8")
    monkeypatch.setenv("SELFEVO_LIVE_SYSLOG_PATH", str(feed))

    events = list(LiveSource())
    assert len(events) == 1
    assert events[0]["logid"] == "0100032002"


def test_live_source_accepts_explicit_path_without_env(tmp_path):
    feed = tmp_path / "explicit.log"
    feed.write_text(SAMPLE_LINES[0] + "\n", encoding="utf-8")
    events = list(LiveSource(path=feed))
    assert len(events) == 1 and events[0]["logid"] == "0100032002"


# --------------------------------------------------------------------------- #
# Safety: the default path neither dials nor imports a network stack          #
# --------------------------------------------------------------------------- #
def test_default_path_opens_no_socket(monkeypatch):
    """Blow up if the replay path tries to open any socket / urlopen."""

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on failure
        raise AssertionError("default incident-source path must not open a connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom, raising=False)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    # Injected batch: must fully parse under the network block.
    injected = list(select_source(lines=SAMPLE_LINES))
    assert [e["action"] for e in injected] == ["login", "perf-stats", "deny"]

    # Default factory source (reads fixtures if present, else empty) -- either
    # way it must not attempt any connection.
    sampled = list(itertools.islice(select_source().events(), 50))
    if FIXTURES_PRESENT:
        assert sampled  # proved we read real bytes with sockets blocked


def test_module_imports_no_network_stack_and_hardcodes_no_host():
    """Static guarantee about the source file itself (belt-and-suspenders)."""
    text = Path(isrc.__file__).read_text(encoding="utf-8")
    banned = [
        "import socket",
        "from socket",
        "socket.socket",
        "import ssl",
        "urllib",
        "http.client",
        "httplib",
        "paramiko",
        "import requests",
        "create_connection",
        "urlopen",
        ".connect(",
        "192.168",
        "R230",
        "telnet",
        "0.0.0.0",
    ]
    hits = [token for token in banned if token in text]
    assert hits == [], f"forbidden network/host tokens in incident_source.py: {hits}"
    # No SSH references, case-insensitively.
    assert "ssh" not in text.lower()
