from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Any


_SERVICE_BY_PORT = {
    22: "ssh",
    53: "dns",
    80: "http",
    443: "https",
    2026: "https-console",
    5432: "postgres",
    6443: "kubernetes-api",
    8026: "http-api",
    8123: "clickhouse-http",
    8443: "admin-https",
    9093: "redpanda-kafka-api",
    9644: "redpanda-admin-api",
    10250: "kubelet-api",
}


@dataclass(frozen=True)
class LiveAsset:
    asset_id: str
    host: str
    ports: tuple[int, ...]


class LiveReadonlyTargetAdapter:
    """Bounded TCP/TLS inspection for explicitly allowlisted private assets.

    The adapter never discovers new targets, submits credentials, sends exploit
    payloads, or mutates a remote system.  A caller must provide every host and
    port up front; public targets are rejected by default.
    """

    readonly_operations = {"port_scan", "service_enum", "tls_check", "banner_grab", "cve_match"}
    approval_required_operations = {"weak_cred_check", "exploit_probe"}
    operations = readonly_operations | approval_required_operations

    def __init__(
        self,
        assets: dict[str, LiveAsset],
        *,
        timeout_sec: float = 0.35,
        allow_public: bool = False,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.assets = dict(assets)
        self.timeout_sec = float(timeout_sec)
        for case_id, asset in self.assets.items():
            if not case_id or not asset.asset_id:
                raise ValueError("case and asset identifiers must not be empty")
            address = ipaddress.ip_address(asset.host)
            if not allow_public and not (address.is_private or address.is_loopback):
                raise ValueError(f"public target is outside the readonly allowlist boundary: {asset.host}")
            if not asset.ports or any(port < 1 or port > 65535 for port in asset.ports):
                raise ValueError(f"asset {asset.asset_id} has an invalid port allowlist")

    def query(self, case_id: str, operation: str) -> list[dict[str, Any]]:
        if operation in self.approval_required_operations:
            raise PermissionError(f"intrusive operation is not implemented by readonly adapter: {operation}")
        if operation not in self.readonly_operations:
            raise ValueError(f"unknown readonly target operation: {operation}")
        try:
            asset = self.assets[case_id]
        except KeyError as exc:
            raise PermissionError(f"case is not in the explicit asset allowlist: {case_id}") from exc

        open_ports = self._open_ports(asset)
        if operation == "port_scan":
            return [self._port_evidence(asset, port) for port in open_ports]
        if operation in {"service_enum", "banner_grab"}:
            return [self._service_evidence(asset, port) for port in open_ports]
        if operation == "tls_check":
            return [item for port in open_ports if (item := self._tls_evidence(asset, port))]
        # A live CVE match requires a versioned vulnerability database.  Returning
        # no evidence is safer than inferring a vulnerability from an open port.
        return []

    def _open_ports(self, asset: LiveAsset) -> list[int]:
        open_ports: list[int] = []
        for port in sorted(set(asset.ports)):
            try:
                with socket.create_connection((asset.host, port), timeout=self.timeout_sec):
                    open_ports.append(port)
            except OSError:
                continue
        return open_ports

    def _port_evidence(self, asset: LiveAsset, port: int) -> dict[str, Any]:
        service = _SERVICE_BY_PORT.get(port, "unknown-tcp")
        return self._evidence(
            asset,
            port,
            service,
            "live:tcp-connect",
            f"{asset.asset_id} ({asset.host}) accepts a TCP connection on allowlisted port {port} ({service}).",
            {"state": "open", "probe": "tcp_connect_only"},
        )

    def _service_evidence(self, asset: LiveAsset, port: int) -> dict[str, Any]:
        service = _SERVICE_BY_PORT.get(port, "unknown-tcp")
        return self._evidence(
            asset,
            port,
            service,
            "live:service-map",
            f"Allowlisted TCP port {port} on {asset.asset_id} maps to service class {service}; no credential or exploit probe was sent.",
            {"state": "open", "identification": "port_map", "probe": "readonly"},
        )

    def _tls_evidence(self, asset: LiveAsset, port: int) -> dict[str, Any] | None:
        if port not in {443, 2026, 6443, 8443, 9443, 10250}:
            return None
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((asset.host, port), timeout=self.timeout_sec) as raw:
                with context.wrap_socket(raw, server_hostname=asset.host) as tls_socket:
                    protocol = str(tls_socket.version() or "unknown")
                    cipher_info = tls_socket.cipher() or ("unknown", "", 0)
        except (OSError, ssl.SSLError):
            return None
        service = _SERVICE_BY_PORT.get(port, "https")
        return self._evidence(
            asset,
            port,
            service,
            "live:tls-handshake",
            f"{asset.asset_id} completed a readonly TLS handshake on port {port} with {protocol} / {cipher_info[0]}.",
            {
                "tls_status": "observed",
                "protocol": protocol,
                "cipher": str(cipher_info[0]),
                "probe": "tls_handshake_only",
            },
        )

    @staticmethod
    def _evidence(
        asset: LiveAsset,
        port: int,
        service: str,
        source: str,
        summary: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"{asset.asset_id}|{asset.host}|{port}|{source}".encode("utf-8")
        ).hexdigest()[:20]
        data = {
            "asset_id": asset.asset_id,
            "host": asset.host,
            "port": port,
            "service": service,
            **extra,
        }
        return {
            "evidence_id": f"ev-live-{digest}",
            "source": source,
            "summary": summary,
            "host": asset.host,
            "port": port,
            "service": service,
            "data": data,
        }
