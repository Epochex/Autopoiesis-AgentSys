from __future__ import annotations

from types import SimpleNamespace

import pytest

from frontend.gateway.ingest import facts_ingest


def _traffic_line(src: str) -> bytes:
    return (
        f'date=2026-08-23 time=12:00:00 type="traffic" srcip={src} '
        'dstip=192.0.2.10 dstport=443 sentbyte=1 rcvdbyte=2\n'
    ).encode()


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, lines: list[bytes], returncode: int = 0):
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.pid = 1234
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _install_process(monkeypatch, lines: list[bytes], returncode: int = 0):
    proc = _FakeProcess(lines, returncode)
    calls = []

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return proc

    monkeypatch.setattr(facts_ingest.subprocess, "Popen", popen)
    return proc, calls


def test_live_stream_flushes_small_batch_when_interval_elapses(monkeypatch):
    _, calls = _install_process(
        monkeypatch,
        [_traffic_line("192.0.2.1"), _traffic_line("192.0.2.2"), _traffic_line("192.0.2.3")],
    )
    batches: list[list[list]] = []
    pulses = iter([0.0, 1.0, 5.0, 6.0, 7.0])

    with pytest.raises(facts_ingest.RecoverableSSHError, match="status 0"):
        facts_ingest._stream(
            "unused",
            lambda rows: batches.append(list(rows)),
            "live-test",
            live_mode=True,
            clock=lambda: next(pulses),
        )

    assert [len(batch) for batch in batches] == [2, 1]
    assert calls[0][1]["stdin"] is facts_ingest.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is None


def test_live_stream_flushes_read_rows_before_nonzero_disconnect(monkeypatch):
    proc, _ = _install_process(
        monkeypatch,
        [_traffic_line("192.0.2.4"), _traffic_line("192.0.2.5")],
        returncode=255,
    )
    batches: list[list[list]] = []

    with pytest.raises(facts_ingest.RecoverableSSHError, match="status 255"):
        facts_ingest._stream(
            "unused",
            lambda rows: batches.append(list(rows)),
            "live-test",
            live_mode=True,
            clock=lambda: 0.0,
        )

    assert [len(batch) for batch in batches] == [2]
    assert proc.stdout.closed is True


def test_stream_stops_ssh_child_when_insert_fails(monkeypatch):
    proc, _ = _install_process(monkeypatch, [_traffic_line("192.0.2.6")])

    def fail_insert(rows):
        raise ValueError("bad insert")

    with pytest.raises(ValueError, match="bad insert"):
        facts_ingest._stream("unused", fail_insert, "live-test", live_mode=True)

    assert proc.terminated is True
    assert proc.stdout.closed is True


def test_live_disconnect_records_reconnect_and_keeps_running(monkeypatch, capsys):
    statuses: list[dict] = []

    def stream_once(remote_cmd, on_row, label, on_process=None, *, live_mode=False, clock=None):
        assert live_mode is True
        if on_process is not None:
            on_process(SimpleNamespace(pid=4321))
        raise facts_ingest.RecoverableSSHError("SSH live stream ended with status 255")

    class _StopLive(BaseException):
        pass

    def stop_after_first_reconnect(seconds):
        assert seconds == 5
        raise _StopLive

    monkeypatch.setattr(facts_ingest, "_source_position", lambda: {})
    monkeypatch.setattr(facts_ingest, "_stream", stream_once)
    monkeypatch.setattr(facts_ingest, "_write_status", lambda **updates: statuses.append(updates))
    monkeypatch.setattr(facts_ingest.time, "sleep", stop_after_first_reconnect)

    with pytest.raises(_StopLive):
        facts_ingest.live()

    reconnecting = [state for state in statuses if state.get("ssh_connection") == "reconnecting"]
    assert reconnecting[-1]["reconnect_count"] == 1
    assert reconnecting[-1]["last_error_type"] == "RecoverableSSHError"
    assert "SSH source interrupted (SSH live stream ended with status 255)" in capsys.readouterr().err


def test_live_fatal_error_is_logged_and_propagated(monkeypatch, capsys):
    statuses: list[dict] = []

    monkeypatch.setattr(facts_ingest, "_source_position", lambda: {})
    monkeypatch.setattr(
        facts_ingest,
        "_stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad fact row")),
    )
    monkeypatch.setattr(facts_ingest, "_write_status", lambda **updates: statuses.append(updates))

    with pytest.raises(ValueError, match="bad fact row"):
        facts_ingest.live()

    failed = [state for state in statuses if state.get("ssh_connection") == "error"]
    assert failed[-1]["last_error_type"] == "ValueError"
    assert "fatal stream error (ValueError: bad fact row)" in capsys.readouterr().err


def test_ssh_command_is_detached_from_standard_input():
    cmd = facts_ingest._ssh_cmd("tail -F /tmp/example.log")

    ssh_index = cmd.index("ssh")
    assert cmd[ssh_index + 1 : ssh_index + 3] == ["-n", "-T"]


def test_systemd_unit_routes_standard_streams_and_restarts_only_on_failure():
    unit = (
        facts_ingest.Path(__file__).parents[1]
        / "frontend/deploy/systemd/netops-facts-ingest.service"
    ).read_text(encoding="utf-8")

    assert "StandardInput=null" in unit
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "Restart=on-failure" in unit
