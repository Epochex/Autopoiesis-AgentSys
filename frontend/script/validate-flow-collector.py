#!/usr/bin/env python3
"""Fail closed when flow collector listen parameters are invalid."""
from __future__ import annotations

import ipaddress
import os


def validate(address: str, netflow_port: str, sflow_port: str, metrics_address: str) -> None:
    parsed = ipaddress.ip_address(address)
    if parsed.version != 4:
        raise ValueError("FLOW_LISTEN_ADDRESS must be an IPv4 address")
    for name, value in (("FLOW_NETFLOW_PORT", netflow_port), ("FLOW_SFLOW_PORT", sflow_port)):
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError(f"{name} must be between 1 and 65535")
    metrics_host, separator, metrics_port = metrics_address.rpartition(":")
    if not separator or ipaddress.ip_address(metrics_host) not in ipaddress.ip_network("127.0.0.0/8"):
        raise ValueError("FLOW_METRICS_ADDRESS must use a loopback IPv4 address")
    if not 1 <= int(metrics_port) <= 65535:
        raise ValueError("FLOW_METRICS_ADDRESS port must be between 1 and 65535")


if __name__ == "__main__":
    validate(
        os.environ["FLOW_LISTEN_ADDRESS"], os.environ["FLOW_NETFLOW_PORT"],
        os.environ["FLOW_SFLOW_PORT"], os.environ["FLOW_METRICS_ADDRESS"],
    )
