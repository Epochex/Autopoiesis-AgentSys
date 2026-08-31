"""PostgreSQL schema isolation for Autopoiesis-owned memory."""

from __future__ import annotations

import re
from typing import Any


_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_schema_name(schema: str | None) -> str | None:
    """Return a safe PostgreSQL identifier or reject ambiguous configuration."""
    if schema is None:
        return None
    normalized = schema.strip()
    if not normalized:
        return None
    if not _SCHEMA_NAME.fullmatch(normalized):
        raise ValueError(
            "PostgreSQL schema must contain lowercase letters, digits, and underscores"
        )
    return normalized


def enter_schema(connection: Any, schema: str | None) -> None:
    """Create and select one validated application schema for this connection."""
    if schema is None:
        return
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cursor.execute(f"SET search_path TO {schema}")


__all__ = ["enter_schema", "validate_schema_name"]
