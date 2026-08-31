from __future__ import annotations

import pytest

from core.memory.postgres_scope import enter_schema, validate_schema_name


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def test_schema_scope_creates_and_selects_validated_namespace() -> None:
    connection = _Connection()
    enter_schema(connection, validate_schema_name("autopoiesis_production"))
    assert connection.cursor_instance.statements == [
        "CREATE SCHEMA IF NOT EXISTS autopoiesis_production",
        "SET search_path TO autopoiesis_production",
    ]


@pytest.mark.parametrize(
    "schema",
    ["public; DROP TABLE memory_records", "MixedCase", "dash-name", "9leading"],
)
def test_schema_scope_rejects_unsafe_identifiers(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        validate_schema_name(schema)
