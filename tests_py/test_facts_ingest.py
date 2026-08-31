from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from frontend.gateway.ingest import facts_ingest


def _traffic_line(src: str) -> bytes:
    return (
        f'date=2026-08-23 time=12:00:00 type="traffic" subtype="forward" srcip={src} '
        'dstip=192.0.2.10 dstport=443 sentbyte=1 rcvdbyte=2\n'
    ).encode()


def _security_row(line: str, provenance: str = "real") -> dict[str, object]:
    row = facts_ingest.parse_security_event(line, provenance)
    assert row is not None
    return dict(zip(facts_ingest._SECURITY_COLS, row, strict=True))


_ADMIN_FAILED = (
    'date=2026-08-23 time=12:01:00 devname="FGT" devid="FG-1" '
    'logid="0100032002" type="event" subtype="system" level="alert" vd="root" '
    'logdesc="Admin login failed" user="mike" method="https" srcip=198.51.100.7 '
    'dstip=192.0.2.1 action="login" status="failed" reason="name_invalid" '
    'msg="Administrator mike login failed because of invalid user name"'
)

_ADMIN_LOCKOUT = (
    'date=2026-08-23 time=12:02:00 logid="0100032021" type="event" '
    'subtype="system" level="alert" logdesc="Admin login disabled" '
    'srcip=198.51.100.8 action="login" status="failed" reason="exceed_limit" '
    'msg="Login disabled from IP 198.51.100.8 because of 3 bad attempts"'
)


def test_parse_security_event_normalizes_admin_auth_events_and_provenance():
    failed = _security_row(_ADMIN_FAILED, "replay")
    lockout = _security_row(_ADMIN_LOCKOUT, "drill")
    failed_event_id = failed.pop("event_id")
    lockout_event_id = lockout.pop("event_id")

    assert len(str(failed_event_id)) == 64
    assert len(str(lockout_event_id)) == 64

    assert failed == {
        "event_ts": "2026-08-23 12:01:00.000",
        "event_type": "admin_login_failed",
        "severity": "alert",
        "device_name": "FGT",
        "device_id": "FG-1",
        "virtual_domain": "root",
        "srcip": "198.51.100.7",
        "dstip": "192.0.2.1",
        "dstport": 0,
        "username": "mike",
        "method": "https",
        "action": "login",
        "status": "failed",
        "reason": "name_invalid",
        "logid": "0100032002",
        "logdesc": "Admin login failed",
        "message": "Administrator mike login failed because of invalid user name",
        "provenance": "replay",
        "raw_log": _ADMIN_FAILED,
    }
    assert lockout["event_type"] == "admin_login_lockout"
    assert lockout["srcip"] == "198.51.100.8"
    assert lockout["provenance"] == "drill"


def test_parse_security_event_distinguishes_disabled_account_and_management_surface():
    disabled = _security_row(
        'date=2026-08-23 time=12:03:00 type="event" subtype="system" '
        'level="warning" logdesc="Administrator account disabled" user="retired-admin" '
        'action="edit" status="success" msg="Administrator account disabled"'
    )
    probe = _security_row(
        'date=2026-08-23 time=12:04:00 type="traffic" subtype="local" level="notice" '
        'srcip=203.0.113.9 srcintfrole="wan" dstip=192.0.2.1 dstport=443 action="deny"'
    )
    exposure = _security_row(
        'date=2026-08-23 time=12:05:00 type="traffic" subtype="local" level="notice" '
        'srcip=203.0.113.10 srcintfrole="wan" dstip=192.0.2.1 dstport=22 action="accept"'
    )

    assert disabled["event_type"] == "admin_account_disabled"
    assert disabled["username"] == "retired-admin"
    assert probe["event_type"] == "management_probe"
    assert probe["dstport"] == 443
    assert exposure["event_type"] == "management_exposure"


def test_parse_security_event_rejects_unrelated_and_unknown_provenance():
    assert facts_ingest.parse_security_event(
        'date=2026-08-23 time=12:00:00 type="traffic" subtype="forward" '
        'dstport=443 action="accept"'
    ) is None
    with pytest.raises(ValueError, match="unsupported security-event provenance"):
        facts_ingest.parse_security_event(_ADMIN_FAILED, "synthetic")


def test_parse_stream_event_builds_stable_real_envelope():
    line = (
        'date=2026-08-23 time=12:00:00 devname="FGT" devid="FG-1" '
        'type="traffic" subtype="forward" level="notice" srcip=192.0.2.10 '
        'srcport=51000 srcmac="aa:bb:cc:dd:ee:ff" srcintf="port5" '
        'srcintfrole="lan" dstip=198.51.100.20 dstport=443 dstintf="wan1" '
        'dstintfrole="wan" policyid=7 policytype="policy" sessionid=99 proto=6 '
        'action="accept" service="HTTPS" sentbyte=100 rcvdbyte=250 sentpkt=2 rcvdpkt=3'
    )

    first = facts_ingest.parse_stream_event(line)
    second = facts_ingest.parse_stream_event(line)

    assert first is not None
    assert first == second
    assert len(str(first["event_id"])) == 64
    assert first["event_ts"] == "2026-08-23T12:00:00.000Z"
    assert first["source_kind"] == "real"
    assert first["replay"] is False
    assert first["device_key"] == "FG-1"
    assert first["src_device_key"] == "aa:bb:cc:dd:ee:ff"
    assert first["bytes_total"] == 350
    assert first["pkts_total"] == 5
    assert first["parse_status"] == "ok"
    assert "status" not in first
    assert first["event_status"] == ""


def test_fact_row_has_same_stable_event_key_across_retries():
    line = _traffic_line("192.0.2.6").decode()
    first = facts_ingest.parse_line(line)
    second = facts_ingest.parse_line(line)

    assert first is not None
    assert first == second
    row = dict(zip(facts_ingest._COLS, first, strict=True))
    assert len(str(row["event_id"])) == 64
    assert row["srcip"] == "192.0.2.6"


def test_redpanda_outbox_retains_then_deletes_acknowledged_batches(tmp_path, monkeypatch):
    outbox = facts_ingest.RedpandaOutbox(
        enabled=True,
        proxy_url="http://redpanda.invalid:8082",
        directory=tmp_path,
        max_bytes=1_000_000,
    )
    events = [
        {"event_id": "event-1", "event_ts": "2026-08-23T12:00:00Z", "type": "traffic", "subtype": "forward"},
        {"event_id": "event-2", "event_ts": "2026-08-23T12:00:01Z", "type": "event", "subtype": "vpn"},
    ]
    sent: list[list[dict[str, object]]] = []
    monkeypatch.setattr(outbox, "_publish", lambda rows: sent.append(rows))

    outbox.enqueue(events)
    assert outbox.pending()[0] == 1
    stored = next(tmp_path.glob("*.jsonl"))
    assert [json.loads(line)["event_id"] for line in stored.read_text().splitlines()] == [
        "event-1",
        "event-2",
    ]

    assert outbox.drain() == 2
    assert sent == [events]
    assert outbox.pending() == (0, 0)


def test_redpanda_outbox_keeps_batch_when_publish_fails(tmp_path, monkeypatch):
    outbox = facts_ingest.RedpandaOutbox(
        enabled=True,
        proxy_url="http://redpanda.invalid:8082",
        directory=tmp_path,
        max_bytes=1_000_000,
    )
    outbox.enqueue([
        {"event_id": "event-1", "event_ts": "2026-08-23T12:00:00Z", "type": "traffic", "subtype": "forward"}
    ])
    monkeypatch.setattr(
        outbox,
        "_publish",
        lambda rows: (_ for _ in ()).throw(TimeoutError("proxy unavailable")),
    )

    with pytest.raises(TimeoutError, match="proxy unavailable"):
        outbox.drain()
    assert outbox.pending()[0] == 1


def test_live_batch_reaches_stream_before_clickhouse_archive():
    order: list[str] = []

    class _Outbox:
        def enqueue(self, events):
            order.append("outbox")

        def drain(self):
            order.append("redpanda")
            return 1

    published, error = facts_ingest.commit_live_batch_sinks(
        [["fact"]],
        [["security"]],
        [{"event_id": "event-1"}],
        outbox=_Outbox(),
        fact_sink=lambda rows: order.append("clickhouse-facts"),
        security_sink=lambda rows: order.append("clickhouse-security"),
    )

    assert order == ["outbox", "redpanda", "clickhouse-facts", "clickhouse-security"]
    assert published == 1
    assert error is None


def test_live_batch_archives_while_broker_batch_stays_queued(capsys):
    order: list[str] = []

    class _Outbox:
        def enqueue(self, events):
            order.append("outbox")

        def drain(self):
            order.append("redpanda")
            raise TimeoutError("broker unavailable")

    published, error = facts_ingest.commit_live_batch_sinks(
        [["fact"]],
        [],
        [{"event_id": "event-1"}],
        outbox=_Outbox(),
        fact_sink=lambda rows: order.append("clickhouse-facts"),
    )

    assert order == ["outbox", "redpanda", "clickhouse-facts"]
    assert published == 0
    assert error == "TimeoutError"
    assert "publish deferred" in capsys.readouterr().err


def test_redpanda_proxy_accepts_partition_level_batch_ack(tmp_path, monkeypatch):
    outbox = facts_ingest.RedpandaOutbox(
        enabled=True,
        proxy_url="http://redpanda.invalid:8082",
        directory=tmp_path,
        max_bytes=1_000_000,
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"offsets":[{"partition":0,"offset":123}]}'

    monkeypatch.setattr(facts_ingest.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    outbox._publish([
        {"event_id": "event-1"},
        {"event_id": "event-2"},
        {"event_id": "event-3"},
    ])


def test_create_and_insert_security_events_use_independent_table(monkeypatch):
    posts: list[tuple[str, bytes | None]] = []
    monkeypatch.setattr(
        facts_ingest,
        "_ch_post",
        lambda query, data=None: posts.append((query, data)),
    )

    facts_ingest.create_security_events_table()
    facts_ingest.ch_insert_security([facts_ingest.parse_security_event(_ADMIN_FAILED)])

    ddl, _ = posts[0]
    insert, payload = posts[1]
    assert "CREATE TABLE IF NOT EXISTS autopoiesis.security_events" in ddl
    assert "provenance Enum8('real' = 1, 'replay' = 2, 'drill' = 3)" in ddl
    assert "ENGINE = ReplacingMergeTree(ingested_at)" in ddl
    assert "ORDER BY event_id" in ddl
    assert insert.startswith("INSERT INTO autopoiesis.security_events")
    assert payload is not None and b"admin_login_failed" in payload


def test_fact_archive_uses_stable_event_key_and_replacing_engine(monkeypatch):
    posts: list[str] = []
    monkeypatch.setattr(facts_ingest, "_ch_post", lambda query, data=None: posts.append(query))

    facts_ingest.create_facts_table()

    ddl = posts[0]
    assert "CREATE TABLE IF NOT EXISTS autopoiesis.facts_v2" in ddl
    assert "event_id String" in ddl
    assert "ENGINE = ReplacingMergeTree(ingested_at)" in ddl
    assert "ORDER BY event_id" in ddl


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


def test_stream_batches_facts_and_security_events_to_separate_sinks(monkeypatch):
    _install_process(monkeypatch, [_traffic_line("192.0.2.4"), (_ADMIN_FAILED + "\n").encode()])
    fact_batches: list[list[list]] = []
    security_batches: list[list[list]] = []

    inserted = facts_ingest._stream(
        "unused",
        lambda rows: fact_batches.append(list(rows)),
        "batch-test",
        on_security_rows=lambda rows: security_batches.append(list(rows)),
        security_provenance="replay",
    )

    assert inserted == 1
    assert [len(rows) for rows in fact_batches] == [1]
    assert [len(rows) for rows in security_batches] == [1]
    assert security_batches[0][0][2] == "admin_login_failed"
    assert security_batches[0][0][18] == "replay"


def test_finite_source_poll_advances_checkpoint_after_sink_commit(monkeypatch):
    lines = [_traffic_line("192.0.2.4"), (_ADMIN_FAILED + "\n").encode()]
    _install_process(monkeypatch, lines)
    order: list[tuple[str, int]] = []
    start = 300

    inserted = facts_ingest._stream(
        "unused",
        None,
        "source-poll-test",
        on_batch=lambda facts, security, events: order.append(("sink", len(facts) + len(security))),
        source_start_offset=start,
        on_source_progress=lambda offset: order.append(("checkpoint", offset)),
    )

    assert inserted == 1
    assert order[0] == ("sink", 2)
    assert order[1] == ("checkpoint", start + sum(len(line) for line in lines))


def test_finite_source_poll_does_not_checkpoint_partial_line(monkeypatch):
    complete = _traffic_line("192.0.2.4")
    partial = _traffic_line("192.0.2.5").rstrip(b"\n")
    _install_process(monkeypatch, [complete, partial])
    offsets: list[int] = []

    facts_ingest._stream(
        "unused",
        None,
        "source-poll-test",
        on_batch=lambda *_args: None,
        source_start_offset=10,
        on_source_progress=offsets.append,
    )

    assert offsets == [10 + len(complete)]


def test_live_batch_contains_all_real_event_types_for_redpanda(monkeypatch):
    vpn = (
        'date=2026-08-23 time=12:05:00 devid="FG-1" type="event" subtype="vpn" '
        'level="information" logdesc="SSL VPN new connection" action="ssl-new-con"\n'
    ).encode()
    _install_process(monkeypatch, [_traffic_line("192.0.2.4"), vpn])
    batches: list[tuple[list[list], list[list], list[dict[str, object]]]] = []

    inserted = facts_ingest._stream(
        "unused",
        None,
        "stream-batch-test",
        on_batch=lambda facts, security, events: batches.append(
            (list(facts), list(security), list(events))
        ),
    )

    assert inserted == 1
    assert len(batches) == 1
    assert len(batches[0][0]) == 1
    assert batches[0][1] == []
    assert [event["subtype"] for event in batches[0][2]] == ["forward", "vpn"]


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

    def stream_once(
        remote_cmd,
        on_row,
        label,
        on_process=None,
        *,
        live_mode=False,
        clock=None,
        on_security_rows=None,
        security_provenance="real",
        on_batch=None,
        source_start_offset=None,
        on_source_progress=None,
        recoverable_process_error=False,
    ):
        assert live_mode is False
        assert on_security_rows is None
        assert callable(on_batch)
        assert security_provenance == "real"
        assert source_start_offset == 100
        assert callable(on_source_progress)
        assert recoverable_process_error is True
        if on_process is not None:
            on_process(SimpleNamespace(pid=4321))
        raise facts_ingest.RecoverableSSHError("SSH stream exited with status 255")

    class _StopLive(BaseException):
        pass

    def stop_after_first_reconnect(seconds):
        assert seconds == facts_ingest.SOURCE_POLL_SECONDS
        raise _StopLive

    checkpoint = {"source_file_id": "1:2", "source_offset_bytes": 100, "source_path": facts_ingest.R230_LOG}
    monkeypatch.setattr(facts_ingest, "_read_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(facts_ingest, "_source_position", lambda path=facts_ingest.R230_LOG: {
        "source_file_id": "1:2", "source_offset_bytes": 120, "source_mtime_epoch": 1,
    })
    monkeypatch.setattr(facts_ingest, "create_facts_table", lambda: None)
    monkeypatch.setattr(facts_ingest, "create_security_events_table", lambda: None)
    monkeypatch.setattr(facts_ingest, "_stream", stream_once)
    monkeypatch.setattr(facts_ingest, "_write_status", lambda **updates: statuses.append(updates))
    monkeypatch.setattr(facts_ingest.time, "sleep", stop_after_first_reconnect)

    with pytest.raises(_StopLive):
        facts_ingest.live()

    reconnecting = [state for state in statuses if state.get("ssh_connection") == "reconnecting"]
    assert reconnecting[-1]["reconnect_count"] == 1
    assert reconnecting[-1]["last_error_type"] == "RecoverableSSHError"
    assert "SSH source interrupted (SSH stream exited with status 255)" in capsys.readouterr().err


def test_live_fatal_error_is_logged_and_propagated(monkeypatch, capsys):
    statuses: list[dict] = []

    checkpoint = {"source_file_id": "1:2", "source_offset_bytes": 100, "source_path": facts_ingest.R230_LOG}
    monkeypatch.setattr(facts_ingest, "_read_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(facts_ingest, "_source_position", lambda path=facts_ingest.R230_LOG: {
        "source_file_id": "1:2", "source_offset_bytes": 120, "source_mtime_epoch": 1,
    })
    monkeypatch.setattr(facts_ingest, "create_facts_table", lambda: None)
    monkeypatch.setattr(facts_ingest, "create_security_events_table", lambda: None)
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
    assert "fatal ingest error (ValueError: bad fact row)" in capsys.readouterr().err


def test_backfill_creates_and_routes_security_sink(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        facts_ingest.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="fortigate.log.1.gz\nfortigate.log\n"),
    )
    monkeypatch.setattr(
        facts_ingest,
        "create_security_events_table",
        lambda: calls.append(("create",)),
    )

    def fake_stream(command, on_row, label, **kwargs):
        calls.append((command, on_row, label, kwargs))
        return 0

    monkeypatch.setattr(facts_ingest, "_stream", fake_stream)

    facts_ingest.backfill()

    assert calls[0] == ("create",)
    streams = calls[1:]
    assert len(streams) == 2
    assert all('subtype="system"' in call[0] for call in streams)
    assert all(call[1] is facts_ingest.ch_insert for call in streams)
    assert all(callable(call[3]["on_security_rows"]) for call in streams)
    assert all(call[3]["security_provenance"] == "real" for call in streams)


def test_security_backfill_never_writes_historical_facts(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        facts_ingest.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="fortigate.log.1.gz\nfortigate.log\nunrelated.txt\n"
        ),
    )
    monkeypatch.setattr(
        facts_ingest,
        "create_security_events_table",
        lambda: calls.append(("create",)),
    )

    def fake_stream(command, on_row, label, **kwargs):
        calls.append((command, on_row, label, kwargs))
        kwargs["on_security_rows"]([["security-row"]])
        return 0

    monkeypatch.setattr(facts_ingest, "_stream", fake_stream)
    inserted: list[list[list]] = []
    monkeypatch.setattr(
        facts_ingest,
        "ch_insert_security",
        lambda rows: inserted.append(list(rows)),
    )

    facts_ingest.security_backfill()

    assert calls[0] == ("create",)
    streams = calls[1:]
    assert len(streams) == 2
    assert all(call[1] is None for call in streams)
    assert all(call[3]["parse_facts"] is False for call in streams)
    assert all('subtype="system"' in call[0] for call in streams)
    assert all('subtype="local"' in call[0] for call in streams)
    assert len(inserted) == 2


def test_ssh_command_is_detached_from_standard_input():
    cmd = facts_ingest._ssh_cmd("tail -F /tmp/example.log")

    ssh_index = cmd.index("ssh")
    assert cmd[ssh_index + 1 : ssh_index + 3] == ["-n", "-T"]


def test_systemd_unit_routes_standard_streams_and_restarts_only_on_failure():
    unit = (
        facts_ingest.Path(__file__).parents[1]
        / "frontend/deploy/systemd/autopoiesis-facts-ingest.service"
    ).read_text(encoding="utf-8")

    assert "StandardInput=null" in unit
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "Restart=on-failure" in unit
    assert "StateDirectory=autopoiesis-facts-ingest" in unit


def test_redpanda_http_service_targets_the_brokers():
    manifest = (
        facts_ingest.Path(__file__).parents[1]
        / "frontend/deployments/11-data-plane-services.yaml"
    ).read_text(encoding="utf-8")

    assert "name: autopoiesis-redpanda-http" in manifest
    assert "port: 8082" in manifest
    assert "targetPort: 8082" in manifest
    assert "app.kubernetes.io/component: redpanda-statefulset" in manifest
