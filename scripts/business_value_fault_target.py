#!/usr/bin/env python3
"""Isolated service used by the executable business-value acceptance run.

The process exposes a real loopback HTTP endpoint and writes its diagnostic
state to the systemd journal.  It never binds a non-loopback address and has no
dependency on the production gateway.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fail-once", "always-fail", "schema-mismatch"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-file")
    return parser


def _serve(port: int, *, schema_version: str, allow_failure_injection: bool = False) -> None:
    body = json.dumps(
        {
            "ready": True,
            "schema_version": schema_version,
            "expected_schema_version": "v2",
        },
        sort_keys=True,
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/inject-failure" and allow_failure_injection:
                reply = b'{"fault_injected":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
                self.wfile.flush()
                print("CONTROLLED_RECURRENCE_FAILURE", flush=True)
                threading.Timer(0.05, lambda: os._exit(25)).start()
                return
            if self.path not in {"/", "/health"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"FAULT_TARGET_READY port={port} schema_version={schema_version}", flush=True)
    server.serve_forever()


def main() -> int:
    args = _parser().parse_args()
    if args.mode == "always-fail":
        print("CONTROLLED_PERMANENT_START_FAILURE", flush=True)
        return 24
    if args.mode == "schema-mismatch":
        print("CONFIG_SCHEMA_MISMATCH expected=v2 observed=v1", flush=True)
        _serve(args.port, schema_version="v1")
        return 0
    if not args.state_file:
        raise SystemExit("--state-file is required for fail-once")
    marker = Path(args.state_file)
    if not marker.exists():
        marker.write_text("first-start-failed\n", encoding="utf-8")
        print("CONTROLLED_FIRST_START_FAILURE", flush=True)
        return 23
    _serve(args.port, schema_version="v2", allow_failure_injection=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
