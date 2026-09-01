from __future__ import annotations

from pathlib import Path

from frontend.gateway.app.live import _probe_rate


def test_event_rate_prefers_fresh_local_receiver(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "fortigate.log"
    source.write_text(
        "date=2026-09-01 time=12:00:00 srcip=192.168.1.20\n"
        "date=2026-09-01 time=12:00:02 srcip=192.168.1.21\n"
        "date=2026-09-01 time=12:00:04 srcip=192.168.1.22\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FORTIGATE_LOG", str(source))
    monkeypatch.delenv("R230_SSH", raising=False)
    monkeypatch.delenv("R230_PASS", raising=False)

    result = _probe_rate()

    assert result["live"] is True
    assert result["sourceMode"] == "local"
    assert result["lines"] == 3
    assert result["eventsPerSec"] == 0.8


def test_event_rate_does_not_mark_a_frozen_local_copy_live(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "fortigate.log"
    source.write_text(
        "date=2026-06-16 time=12:00:00 srcip=192.168.1.20\n"
        "date=2026-06-16 time=12:00:01 srcip=192.168.1.21\n",
        encoding="utf-8",
    )
    source.touch()
    monkeypatch.setenv("FORTIGATE_LOG", str(source))
    monkeypatch.delenv("R230_SSH", raising=False)
    monkeypatch.delenv("R230_PASS", raising=False)
    monkeypatch.setattr("frontend.gateway.app.live.time.time", lambda: source.stat().st_mtime + 121)

    result = _probe_rate()

    assert result["live"] is False
    assert result["sourceMode"] == "local"
