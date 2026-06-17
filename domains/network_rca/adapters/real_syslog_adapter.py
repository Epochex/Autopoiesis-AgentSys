from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RealSyslogAdapter:
    """Readonly evidence source backed by real FortiGate syslog aggregates.

    The authoritative aggregates in ``stats`` are computed over the FULL R230
    capture (see ``scripts``/manifest notes), not over the sampled log copies
    committed locally. Evidence items are derived from those real aggregates,
    so held-out metrics reflect real device data rather than handwritten mocks.
    Keyed by operation (not case id) so it cannot be overfit to specific cases.
    """

    def __init__(self, stats: dict[str, Any]):
        self.stats = stats

    @classmethod
    def from_path(cls, stats_path: str | Path) -> "RealSyslogAdapter":
        return cls(json.loads(Path(stats_path).read_text(encoding="utf-8")))

    def query(self, case_id: str, operation: str) -> list[dict]:
        builder = _OPERATIONS.get(operation)
        if builder is None:
            return []
        return builder(self.stats)


def _top(pairs: list, n: int = 3) -> list:
    return [list(item) for item in (pairs or [])[:n]]


def _ev_admin_auth_failures(s: dict) -> list[dict]:
    count = int(s.get("admin_login_failed", 0))
    distinct = int(s.get("admin_login_failed_distinct_src", 0))
    top = _top(s.get("admin_login_failed_top_src", []))
    return [
        {
            "evidence_id": "ev-admin-auth-failures",
            "source": "fortigate_syslog:event/system logdesc=\"Admin login failed\"",
            "summary": (
                f"{count} failed admin logins from {distinct} distinct source IPs "
                f"(top external sources {[ip for ip, _ in (s.get('admin_login_failed_top_src') or [])][:3]})"
            ),
            "data": {
                "failed_login_count": count,
                "distinct_src_count": distinct,
                "top_src": top,
            },
        }
    ]


def _ev_admin_lockout(s: dict) -> list[dict]:
    lockouts = int(s.get("admin_login_disabled_lockouts", 0))
    return [
        {
            "evidence_id": "ev-admin-lockout",
            "source": "fortigate_syslog:event/system logdesc=\"Admin login disabled\"",
            "summary": f"{lockouts} admin-login-disabled lockout events triggered by repeated failures",
            "data": {"lockout_events": lockouts},
        }
    ]


def _ev_policy_deny_profile(s: dict) -> list[dict]:
    deny = int(s.get("deny_count", 0))
    top_ports = _top(s.get("deny_top_dstports", []))
    top_src = _top(s.get("deny_top_src", []))
    internal = [ip for ip, _ in (s.get("deny_top_src") or []) if str(ip).startswith("192.168.")]
    internal_ratio = round(len(internal) / max(1, len(s.get("deny_top_src") or [])), 3)
    return [
        {
            "evidence_id": "ev-policy-deny-profile",
            "source": "fortigate_syslog:traffic action=\"deny\"",
            "summary": (
                f"{deny} denied flows; top destination ports {[p for p, _ in (s.get('deny_top_dstports') or [])][:3]}; "
                f"top sources are internal hosts ({internal_ratio} of top sources are 192.168.0.0/16)"
            ),
            "data": {
                "deny_count": deny,
                "top_dstports": top_ports,
                "top_src": top_src,
                "internal_src_ratio": internal_ratio,
            },
        }
    ]


def _ev_traffic_baseline(s: dict) -> list[dict]:
    accept = int(s.get("accept_permit_count", 0))
    deny = int(s.get("deny_count", 0))
    ratio = round(deny / max(1, accept), 2)
    return [
        {
            "evidence_id": "ev-traffic-baseline",
            "source": "fortigate_syslog:traffic action=\"accept|permit\"",
            "summary": f"{accept} accepted/permitted flows vs {deny} denied (deny:accept ratio {ratio})",
            "data": {"accept_permit_count": accept, "deny_to_accept_ratio": ratio},
        }
    ]


def _ev_event_log_scan(s: dict) -> list[dict]:
    return [
        {
            "evidence_id": "ev-event-log-scan",
            "source": "fortigate_syslog:event/system logdesc scan",
            "summary": (
                f"{int(s.get('session_clash', 0))} informational session-clash events, "
                f"{int(s.get('fortigate_update_succeeded', 0))} successful FortiGuard updates"
            ),
            "data": {
                "session_clash": int(s.get("session_clash", 0)),
                "fortigate_update_succeeded": int(s.get("fortigate_update_succeeded", 0)),
            },
        }
    ]


_OPERATIONS = {
    "admin_auth_failures": _ev_admin_auth_failures,
    "admin_lockout": _ev_admin_lockout,
    "policy_deny_profile": _ev_policy_deny_profile,
    "traffic_baseline": _ev_traffic_baseline,
    "event_log_scan": _ev_event_log_scan,
}
