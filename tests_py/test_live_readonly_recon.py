from __future__ import annotations

import socket
import threading

import pytest

from domains.active_recon.adapters.live_readonly_target import LiveAsset, LiveReadonlyTargetAdapter
from domains.active_recon.live_scan import scan_assets


def _listening_socket() -> tuple[socket.socket, int, threading.Event, threading.Thread]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    stop = threading.Event()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                pass

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    return server, int(server.getsockname()[1]), stop, thread


def test_live_adapter_scans_only_explicit_private_allowlist() -> None:
    server, open_port, stop, thread = _listening_socket()
    try:
        adapter = LiveReadonlyTargetAdapter(
            {"owned-node": LiveAsset("owned-node", "127.0.0.1", (open_port,))},
            timeout_sec=0.2,
        )
        evidence = adapter.query("owned-node", "port_scan")
        assert len(evidence) == 1
        assert evidence[0]["port"] == open_port
        assert evidence[0]["source"] == "live:tcp-connect"
        assert evidence[0]["data"]["probe"] == "tcp_connect_only"
    finally:
        stop.set()
        thread.join(timeout=1)
        server.close()


def test_live_adapter_rejects_unknown_asset_and_intrusive_operations() -> None:
    adapter = LiveReadonlyTargetAdapter(
        {"owned-node": LiveAsset("owned-node", "127.0.0.1", (22,))}
    )
    with pytest.raises(PermissionError, match="allowlist"):
        adapter.query("other-node", "port_scan")
    with pytest.raises(PermissionError, match="not implemented"):
        adapter.query("owned-node", "exploit_probe")
    with pytest.raises(PermissionError, match="not implemented"):
        adapter.query("owned-node", "weak_cred_check")


def test_live_adapter_rejects_public_targets_by_default() -> None:
    with pytest.raises(ValueError, match="public target"):
        LiveReadonlyTargetAdapter(
            {"external": LiveAsset("external", "8.8.8.8", (53,))}
        )


def test_scan_report_records_guardrails_and_evidence() -> None:
    server, open_port, stop, thread = _listening_socket()
    try:
        report = scan_assets(
            {"owned-node": LiveAsset("owned-node", "127.0.0.1", (open_port,))},
            timeout_sec=0.2,
        )
        assert report["mode"] == "allowlisted_readonly_tcp_connect"
        assert report["guardrails"]["exploit_probe"] is False
        assert report["assets"][0]["open_services"][0]["port"] == open_port
        assert report["assets"][0]["open_services"][0]["evidence_id"].startswith("ev-live-")
    finally:
        stop.set()
        thread.join(timeout=1)
        server.close()
