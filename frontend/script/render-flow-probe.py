#!/usr/bin/env python3
"""Render a bounded pmacctd IPFIX endpoint-probe configuration."""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
from pathlib import Path

INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


def render(interface: str, collector: str, port: int, network: str = "") -> str:
    if not INTERFACE.fullmatch(interface):
        raise ValueError("interface must be a 1-32 character Linux interface name")
    collector_ip = str(ipaddress.ip_address(collector))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    export_filter = f"not (dst host {collector_ip} and udp dst port {port})"
    if network:
        capture_filter = (
            "pcap_filter: "
            f"(net {ipaddress.ip_network(network, strict=False)}) and {export_filter}\n"
        )
    else:
        capture_filter = f"pcap_filter: {export_filter}\n"
    return (
        "daemonize: false\n"
        "promisc: true\n"
        f"pcap_interface: {interface}\n"
        f"{capture_filter}"
        "plugins: nfprobe[autopoiesis]\n"
        "aggregate[autopoiesis]: src_host, dst_host, src_port, dst_port, proto, tos, src_mac, dst_mac, vlan\n"
        f"nfprobe_receiver[autopoiesis]: {collector_ip}:{port}\n"
        "nfprobe_version[autopoiesis]: 10\n"
        "nfprobe_timeouts[autopoiesis]: tcp=30:tcp.rst=5:tcp.fin=5:udp=15:icmp=15:general=30:maxlife=60:expint=5\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument("--collector", required=True)
    parser.add_argument("--port", type=int, default=2055)
    parser.add_argument("--network", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(render(args.interface, args.collector, args.port, args.network), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, output)


if __name__ == "__main__":
    main()
