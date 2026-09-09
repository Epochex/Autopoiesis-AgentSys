"""Shared classification for gateway administrator authentication events."""
from __future__ import annotations

from typing import Any, Mapping


def classify_admin_auth_event(event: Mapping[str, Any]) -> str | None:
    """Return the normalized security event type used by detection and history.

    The live detector and the ClickHouse writer call this same function so an
    incident count can always be reproduced from its archived event window.
    """
    if str(event.get("type") or "").casefold() != "event":
        return None
    if str(event.get("subtype") or "").casefold() != "system":
        return None
    combined = " ".join(
        str(event.get(field) or "")
        for field in (
            "logdesc", "msg", "action", "event_status", "status", "reason", "user",
        )
    ).casefold()
    action = str(event.get("action") or "").casefold()
    reason = str(event.get("reason") or "").casefold()
    admin_context = "admin" in combined or "administrator" in combined
    login_context = "login" in combined or action == "login"
    if not admin_context:
        return None
    account_disabled = bool(
        ("account" in combined or "user" in combined)
        and "disabled" in combined
        and not login_context
    )
    if account_disabled:
        return "admin_account_disabled"
    lockout = bool(
        login_context
        and (
            "disabled" in combined
            or "lockout" in combined
            or "locked out" in combined
            or reason == "exceed_limit"
        )
    )
    if lockout:
        return "admin_login_lockout"
    failed = bool(
        login_context
        and any(marker in combined for marker in ("failed", "failure", "invalid"))
    )
    return "admin_login_failed" if failed else None


__all__ = ["classify_admin_auth_event"]
