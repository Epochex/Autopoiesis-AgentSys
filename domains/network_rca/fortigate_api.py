"""FortiGate REST 只读采集，供网络拓扑、设备识别和变更账使用。"""

from __future__ import annotations

import base64
import json
import os
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Any, Protocol
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
    urlopen,
)

from core.env import autopoiesis_env


_PURPOSES = {
    "interfaces": "网络拓扑：说明设备挂在哪个接口及其 VLAN 归属",
    "devices": "设备识别：把日志中的裸 IP 对应到 MAC 和主机名",
    "policies": "流量解释：用源区、目的区和动作说明 deny 的策略上下文",
    "changes": "变更账：说明谁在什么时候改了什么",
}


class ReadonlyHttpClient(Protocol):
    """很小的注入边界让单测能替换网络，同时限制实现只表达读取。"""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Mapping[str, Any]: ...


class UrlLibReadonlyHttpClient:
    """标准库 HTTP 实现，避免为了四个读取端点增加项目依赖。"""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Mapping[str, Any]:
        request = Request(url, headers=dict(headers), method="GET")
        # 自签证书场景只能由调用方显式关闭校验；关闭后无法确认对端确是目标防火墙。
        context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, list):
            return {"status": "success", "results": payload}
        if not isinstance(payload, Mapping):
            raise ValueError("FortiGate response is not a JSON object")
        return payload


class FortiOSCookieReadonlyHttpClient:
    """FortiOS web-session transport whose data operations remain GET-only.

    Older FortiOS releases used in the local network reject HTTP Basic auth on
    API endpoints while accepting the same account through ``/logincheck``.
    Keeping that authentication detail in the transport lets the domain client
    expose one consistent read contract instead of silently returning empty
    topology, policy and change data on those releases.
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._lock = threading.Lock()

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Mapping[str, Any]:
        parsed_base = urlsplit(self._base_url)
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != parsed_base.netloc
            or not parsed_url.path.startswith("/api/v2/")
        ):
            raise ValueError("FortiGate read URL is outside the configured API origin")

        context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        jar = CookieJar()
        opener = build_opener(
            HTTPSHandler(context=context),
            HTTPCookieProcessor(jar),
        )
        # Serialising login/logout avoids invalidating a cookie while another
        # request made by the same account is still reading its response.
        with self._lock:
            csrf = ""
            try:
                body = urlencode(
                    {"username": self._username, "secretkey": self._password}
                ).encode("utf-8")
                login = Request(f"{self._base_url}/logincheck", data=body, method="POST")
                with opener.open(login, timeout=timeout) as response:
                    if response.status != 200:
                        raise PermissionError("FortiGate login was rejected")
                for cookie in jar:
                    if cookie.name.startswith("ccsrftoken") and cookie.value:
                        csrf = unquote(cookie.value).strip('"')
                        break
                if not csrf:
                    raise PermissionError("FortiGate login returned no CSRF token")

                read_headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() != "authorization"
                }
                read_headers["X-CSRFTOKEN"] = csrf
                request = Request(url, headers=read_headers, method="GET")
                with opener.open(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, list):
                    return {"status": "success", "results": payload}
                if not isinstance(payload, Mapping):
                    raise ValueError("FortiGate response is not a JSON object")
                return payload
            finally:
                if csrf:
                    try:
                        logout = Request(
                            f"{self._base_url}/logout",
                            data=b"",
                            headers={"X-CSRFTOKEN": csrf},
                            method="POST",
                        )
                        opener.open(logout, timeout=min(timeout, 4.0)).close()
                    except Exception:
                        pass


class FortiGateReadonlyAPI:
    """只读 FortiGate REST 客户端。

    ``verify_tls`` 默认开启。设备仅有自签证书时可以显式传入 ``False``，代价是连接
    无法抵御中间人替换证书。用户名和口令只进入认证头，不进入对象展示或错误结果。
    """

    _INTERFACES_PATH = "/api/v2/cmdb/system/interface?vdom=*"
    _DHCP_PATH = "/api/v2/monitor/system/dhcp?scope=global"
    _DEVICES_PATHS = (
        "/api/v2/monitor/user/device/select",
        "/api/v2/monitor/user/device/query?number=2000",
        "/api/v2/monitor/user/device/query?vdom=*",
        "/api/v2/monitor/user/detected-device",
    )
    _POLICIES_PATH = "/api/v2/cmdb/firewall/policy?vdom=*"
    _CHANGES_PATH = "/api/v2/monitor/system/config-revision?scope=global"

    def __init__(
        self,
        base_url: str | None,
        username: str | None,
        password: str | None,
        *,
        http_client: ReadonlyHttpClient | None = None,
        timeout: float = 5.0,
        retries: int = 2,
        retry_delay: float = 0.1,
        verify_tls: bool = True,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries must not be negative")
        if retry_delay < 0:
            raise ValueError("retry_delay must not be negative")

        clean_base = (base_url or "").strip().rstrip("/")
        if clean_base:
            parsed = urlsplit(clean_base)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("FortiGate base URL must be an HTTPS origin")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("FortiGate base URL must not contain credentials")

        self._base_url = clean_base
        self._username = username or ""
        self._password = password or ""
        self._http = http_client or UrlLibReadonlyHttpClient()
        self._timeout = float(timeout)
        self._retries = int(retries)
        self._retry_delay = float(retry_delay)
        self._verify_tls = bool(verify_tls)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep

    @classmethod
    def from_env(cls, **options: Any) -> "FortiGateReadonlyAPI":
        """从统一环境入口读取凭据，并兼容部署文件现有的裸 ``FGT_*`` 名称。"""
        base_url = autopoiesis_env("FGT_BASE", os.environ.get("FGT_BASE"))
        username = autopoiesis_env("FGT_USER", os.environ.get("FGT_USER"))
        password = autopoiesis_env("FGT_PASS", os.environ.get("FGT_PASS"))
        if "http_client" not in options and base_url and username and password:
            options["http_client"] = FortiOSCookieReadonlyHttpClient(
                base_url, username, password
            )
        return cls(
            base_url,
            username,
            password,
            **options,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"username='<redacted>', password='<redacted>', "
            f"verify_tls={self._verify_tls!r}, timeout={self._timeout!r}, "
            f"retries={self._retries!r})"
        )

    def fetch_interfaces(self) -> dict[str, Any]:
        """读取接口、地址、角色和 VLAN 归属，组成拓扑的接入口事实。"""

        payload, reason = self._read_remote(self._INTERFACES_PATH)
        if payload is None:
            return self._missing_result("interfaces", "interfaces", reason)
        rows = _extract_rows(payload, identity_keys={"name", "interface", "ip", "vlanid"})
        items = [_interface_row(row) for row in rows]
        return self._available_result("interfaces", items)

    def fetch_devices(self) -> dict[str, Any]:
        """合并租约和已知设备，给 syslog 中只出现 IP 的事件补上设备身份。"""

        sources = (
            ("dhcp_leases", (self._DHCP_PATH,), "dhcp_lease"),
            ("known_devices", self._DEVICES_PATHS, "known_device"),
        )
        merged: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        missing: list[dict[str, str]] = []
        for item_name, paths, source in sources:
            payload, reason = self._read_first(paths)
            if payload is None:
                missing.append({"item": item_name, "reason": reason})
                continue
            rows = _extract_rows(
                payload,
                identity_keys={
                    "mac",
                    "macaddr",
                    "mac_address",
                    "ip",
                    "ip_address",
                    "ipv4_address",
                    "hostname",
                },
            )
            for row in rows:
                device = _device_row(row, source)
                key = (device["mac"], device["ip"])
                previous = merged.get(key)
                if previous is None:
                    merged[key] = device
                    continue
                if previous["hostname"] is None and device["hostname"] is not None:
                    previous["hostname"] = device["hostname"]
                previous["sources"] = sorted(set(previous["sources"] + device["sources"]))
                previous["missing_fields"] = _missing_fields(
                    previous, ("mac", "ip", "hostname")
                )

        if len(missing) == len(sources):
            return self._result(
                "devices",
                available=False,
                degraded=True,
                items=None,
                missing=missing,
            )
        return self._result(
            "devices",
            available=True,
            degraded=bool(missing),
            items=list(merged.values()),
            missing=missing,
        )

    def fetch_policies(self) -> dict[str, Any]:
        """读取策略最小摘要，让 deny 事件能对应到源区、目的区和动作。"""

        payload, reason = self._read_remote(self._POLICIES_PATH)
        if payload is None:
            return self._missing_result("policies", "policy_summaries", reason)
        rows = _extract_rows(payload, identity_keys={"policyid", "policy_id", "srcintf", "dstintf"})
        items = [_policy_row(row) for row in rows]
        return self._available_result("policies", items)

    def fetch_change_ledger(self) -> dict[str, Any]:
        """读取配置修订记录，保留版本、操作者、时间和变更说明。"""

        payload, reason = self._read_remote(self._CHANGES_PATH)
        if payload is None:
            return self._missing_result("changes", "config_revisions", reason)
        rows = _extract_rows(
            payload,
            identity_keys={"version", "revision", "created", "timestamp", "administrator", "user"},
        )
        items = [_change_row(row) for row in rows]
        return self._available_result("changes", items)

    def fetch_all(self) -> dict[str, Any]:
        """分别读取四类事实，某一端点失败时保留其余成功结果。"""

        results = {
            "interfaces": self.fetch_interfaces(),
            "devices": self.fetch_devices(),
            "policies": self.fetch_policies(),
            "changes": self.fetch_change_ledger(),
        }
        return {
            "fetched_at": self._timestamp(),
            "degraded": any(result["degraded"] for result in results.values()),
            **results,
        }

    def _read_remote(self, path: str) -> tuple[Mapping[str, Any] | None, str]:
        if not self._base_url or not self._username or not self._password:
            return None, "FortiGate credentials are not configured"

        raw = f"{self._username}:{self._password}".encode("utf-8")
        authorization = base64.b64encode(raw).decode("ascii")
        headers = {"Accept": "application/json", "Authorization": f"Basic {authorization}"}
        for attempt in range(self._retries + 1):
            try:
                payload = self._http.get_json(
                    f"{self._base_url}{path}",
                    headers=headers,
                    timeout=self._timeout,
                    verify_tls=self._verify_tls,
                )
                status = str(payload.get("status", "success")).lower()
                if status != "success":
                    return None, "FortiGate reported that this item is unavailable"
                return payload, ""
            except Exception:
                # 底层异常可能带请求头或 URL；统一错误文案可以避免凭据被日志继续传播。
                if attempt < self._retries:
                    self._sleeper(self._retry_delay * (2**attempt))
        return None, f"FortiGate request failed after {self._retries + 1} attempt(s)"

    def _read_first(
        self, paths: Sequence[str]
    ) -> tuple[Mapping[str, Any] | None, str]:
        """Read the first endpoint supported by this FortiOS release."""
        reason = "FortiGate reported that this item is unavailable"
        for path in paths:
            payload, reason = self._read_remote(path)
            if payload is not None:
                return payload, ""
        return None, reason

    def _available_result(self, kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._result(kind, available=True, degraded=False, items=items, missing=[])

    def _missing_result(self, kind: str, item: str, reason: str) -> dict[str, Any]:
        return self._result(
            kind,
            available=False,
            degraded=True,
            items=None,
            missing=[{"item": item, "reason": reason}],
        )

    def _result(
        self,
        kind: str,
        *,
        available: bool,
        degraded: bool,
        items: list[dict[str, Any]] | None,
        missing: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "purpose": _PURPOSES[kind],
            "fetched_at": self._timestamp(),
            "available": available,
            "degraded": degraded,
            "items": items,
            "missing": missing,
        }

    def _timestamp(self) -> str:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_rows(payload: Mapping[str, Any], *, identity_keys: set[str]) -> list[Mapping[str, Any]]:
    """FortiOS 各版本会在 results、VDOM、devices 等层级间移动列表。"""

    root: Any = payload.get("results", payload)
    rows: list[Mapping[str, Any]] = []
    container_keys = {"results", "data", "entries", "leases", "devices", "revisions", "records"}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            # 有些容器也带 firmware version 或接口名；只在没有已知子容器时把它当数据行。
            if identity_keys.intersection(value) and not container_keys.intersection(value):
                rows.append(value)
                return
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for nested in value:
                visit(nested)

    visit(root)
    return rows


def _interface_row(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        "name": _first_text(row, "name", "interface", "interface_name"),
        "status": _first_text(row, "status", "link", "link_status"),
        "ip": _ip_value(row.get("ip", row.get("ip_address"))),
        "role": _first_text(row, "role", "interface_role"),
        "vlan_id": _first_value(row, "vlanid", "vlan_id"),
        "parent_interface": _first_text(row, "interface", "parent", "parent_interface"),
    }
    item["missing_fields"] = _missing_fields(item, ("name", "status", "ip", "role", "vlan_id"))
    return item


def _device_row(row: Mapping[str, Any], source: str) -> dict[str, Any]:
    item = {
        "mac": _first_text(row, "mac", "macaddr", "mac_address"),
        "ip": _first_text(row, "ip", "ip_address", "ipv4_address"),
        "hostname": _first_text(row, "hostname", "host", "device_name", "devname", "name"),
        "sources": [source],
    }
    item["missing_fields"] = _missing_fields(item, ("mac", "ip", "hostname"))
    return item


def _policy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        "id": _first_value(row, "policyid", "policy_id", "id"),
        "source_zones": _names(row.get("srcintf", row.get("source_zone"))),
        "destination_zones": _names(row.get("dstintf", row.get("destination_zone"))),
        "action": _first_text(row, "action"),
    }
    item["missing_fields"] = _missing_fields(
        item, ("id", "source_zones", "destination_zones", "action")
    )
    return item


def _change_row(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        "version": _first_value(row, "version", "revision", "id"),
        "changed_at": _first_value(row, "timestamp", "created", "date", "time"),
        "administrator": _first_text(row, "administrator", "admin", "user", "username"),
        "summary": _first_text(row, "summary", "comment", "comments", "description", "config"),
    }
    item["missing_fields"] = _missing_fields(
        item, ("version", "changed_at", "administrator", "summary")
    )
    return item


def _first_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "N/A"):
            return value
    return None


def _first_text(row: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(row, *keys)
    return str(value) if value is not None else None


def _ip_value(value: Any) -> str | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = [str(part) for part in value if part not in (None, "", "N/A")]
        return "/".join(values) if values else None
    return str(value) if value not in (None, "", "N/A") else None


def _names(value: Any) -> list[str] | None:
    if value in (None, "", "N/A"):
        return None
    raw_values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    names: list[str] = []
    for raw in raw_values:
        if isinstance(raw, Mapping):
            name = _first_text(raw, "name", "q_origin_key", "interface")
        else:
            name = str(raw)
        if name:
            names.append(name)
    return names or None


def _missing_fields(item: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    return [field for field in fields if item.get(field) in (None, "", [])]


__all__ = [
    "FortiGateReadonlyAPI",
    "FortiOSCookieReadonlyHttpClient",
    "ReadonlyHttpClient",
    "UrlLibReadonlyHttpClient",
]
