"""Frozen, deterministic checks for model-proposed investigation probes.

An open-ended root cause is useful only when it can be falsified. The model
declares these checks before the command is executed. This module evaluates the
saved declaration against the later command output, so the same bytes cannot be
reinterpreted after the result is known.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


PredicateOperator = Literal[
    "contains",
    "not_contains",
    "regex",
    "not_regex",
    "equals",
    "not_equals",
]


class ObservationPredicate(BaseModel):
    """One bounded boolean condition over a successful probe's stdout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator: PredicateOperator
    value: str
    case_sensitive: bool = False

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("predicate value must contain at least two characters")
        if len(value) > 240:
            raise ValueError("predicate value exceeds 240 characters")
        return value

    @model_validator(mode="after")
    def valid_regex(self) -> "ObservationPredicate":
        if self.operator in {"regex", "not_regex"}:
            try:
                re.compile(self.value)
            except re.error as error:
                raise ValueError(f"invalid predicate regex: {error}") from error
        return self


def evaluate_observation(
    predicate: ObservationPredicate,
    *,
    output: str,
    ok: bool,
) -> bool | None:
    """Return true/false for an observed result, or None for tool failure."""

    if not ok:
        return None
    actual = output if predicate.case_sensitive else output.casefold()
    expected = predicate.value if predicate.case_sensitive else predicate.value.casefold()
    if predicate.operator == "contains":
        return expected in actual
    if predicate.operator == "not_contains":
        return expected not in actual
    if predicate.operator == "equals":
        return actual.strip() == expected
    if predicate.operator == "not_equals":
        return actual.strip() != expected
    flags = 0 if predicate.case_sensitive else re.IGNORECASE
    matched = re.search(predicate.value, output, flags=flags) is not None
    return matched if predicate.operator == "regex" else not matched


__all__ = ["ObservationPredicate", "PredicateOperator", "evaluate_observation"]
