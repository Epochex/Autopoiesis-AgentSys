"""What a spec under test is, and what a generated test case is.

The boundary vocabulary lives here rather than in the generator or the scorer
because both need it and they must agree: the generator emits a value for a
role, the scorer reads a role back off a value. If those two disagreed, a suite
could earn boundary credit for values that are not on any boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ParamType = Literal["int", "float", "str", "bool", "enum"]
TestKind = Literal["happy_path", "boundary", "negative", "error_path", "idempotency"]

# The four positions boundary value analysis cares about. `just_outside` is one
# step past a bound, not any out-of-range value: a wildly out-of-range input
# exercises the same branch a type error does, and proves nothing about an
# off-by-one in the guard.
BoundaryRole = Literal["min", "max", "just_inside", "just_outside"]
BOUNDARY_ROLES: tuple[str, ...] = ("min", "max", "just_inside", "just_outside")


class ParamSpec(BaseModel):
    """One declared parameter: its type, whether it is required, and its constraints."""

    name: str = Field(min_length=1)
    type: ParamType
    required: bool = True
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    enum_values: list[str] = Field(default_factory=list)
    # A realistic in-range value for the happy path. Derived when absent, but a
    # derived value ("aaaa") makes a generated suite unreadable to a human.
    example: Any = None
    description: str = ""

    @property
    def constrained(self) -> bool:
        """True when the parameter has at least one boundary a test can sit on."""
        return bool(self.boundary_roles())

    def boundary_roles(self) -> dict[str, Any]:
        """Map each *reachable* boundary role to one concrete value.

        Reachability matters: a two-member enum has no interior, and an integer
        bounded to [1, 2] has no strictly-inside value. Scoring a suite against
        roles it could never fill would report a permanent, unfixable gap.
        """
        if self.type == "enum":
            return self._enum_roles()
        if self.type in {"int", "float"}:
            return self._numeric_roles()
        if self.type == "str":
            return self._length_roles()
        return {}

    def required_boundary_roles(self) -> tuple[str, ...]:
        """The roles a complete suite must cover for this parameter."""
        return tuple(role for role in BOUNDARY_ROLES if role in self.boundary_roles())

    def role_of(self, value: Any) -> str | None:
        """Classify `value` by where it sits relative to this parameter's bounds.

        Read off the value itself, never off a case's own label — a case that
        calls itself a boundary test while passing a mid-range value must earn
        nothing. Returns None for a value that occupies no boundary position.
        """
        if self.type == "enum":
            return self._enum_role_of(value)
        if self.type in {"int", "float"}:
            return self._numeric_role_of(value)
        if self.type == "str":
            return self._length_role_of(value)
        return None

    def satisfies(self, value: Any) -> bool:
        """True when `value` is a legal input for this parameter."""
        if self.type == "enum":
            return value in self.enum_values
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            return self._within(float(value))
        if self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            return self._within(float(value))
        if not isinstance(value, str):
            return False
        return self._within_length(len(value))

    def typical_value(self) -> Any:
        """A legal, unremarkable value: what the happy path should pass."""
        if self.example is not None:
            return self.example
        if self.type == "enum":
            return self.enum_values[0] if self.enum_values else ""
        if self.type == "bool":
            return True
        if self.type in {"int", "float"}:
            return self._typical_number()
        length = self._typical_length()
        return "x" * length

    def wrong_type_value(self) -> Any:
        """A value of the wrong type — for the negative case the type guard owns."""
        if self.type in {"int", "float"}:
            return "not-a-number"
        if self.type == "bool":
            return "maybe"
        # An out-of-enum *string* is a boundary position, not a type error, so an
        # enum's type violation has to be a non-string.
        return 0 if self.type == "enum" else -1

    def _within(self, value: float) -> bool:
        if self.minimum is not None and value < self.minimum:
            return False
        return not (self.maximum is not None and value > self.maximum)

    def _within_length(self, length: int) -> bool:
        if self.min_length is not None and length < self.min_length:
            return False
        return not (self.max_length is not None and length > self.max_length)

    def _typical_number(self) -> Any:
        low, high = self.minimum, self.maximum
        if low is not None and high is not None:
            middle = (low + high) / 2
        elif low is not None:
            middle = low + 1
        elif high is not None:
            middle = high - 1
        else:
            middle = 1
        return int(middle) if self.type == "int" else float(middle)

    def _typical_length(self) -> int:
        low = self.min_length if self.min_length is not None else 1
        high = self.max_length if self.max_length is not None else low + 5
        return max(low, min(high, low + 4))

    def _numeric_roles(self) -> dict[str, Any]:
        low, high = self.minimum, self.maximum
        if low is None and high is None:
            return {}
        cast = int if self.type == "int" else float
        roles: dict[str, Any] = {}
        if low is not None:
            roles["min"] = cast(low)
        if high is not None:
            roles["max"] = cast(high)
        # A float has no "next" value, so the inside/outside step is only defined
        # for integers; a float parameter is scored on min/max plus out-of-range.
        if self.type == "int":
            inside = self._interior_int(low, high)
            if inside is not None:
                roles["just_inside"] = inside
            roles["just_outside"] = int(low) - 1 if low is not None else int(high) + 1
        elif low is not None or high is not None:
            roles["just_outside"] = (low - 1.0) if low is not None else (high + 1.0)
        return roles

    @staticmethod
    def _interior_int(low: float | None, high: float | None) -> int | None:
        if low is not None and (high is None or low + 1 < high):
            return int(low) + 1
        if high is not None and (low is None or high - 1 > low):
            return int(high) - 1
        return None

    def _length_roles(self) -> dict[str, Any]:
        low, high = self.min_length, self.max_length
        if low is None and high is None:
            return {}
        roles: dict[str, Any] = {}
        if low is not None:
            roles["min"] = "x" * low
        if high is not None:
            roles["max"] = "x" * high
        inside = self._interior_int(low, high)
        if inside is not None:
            roles["just_inside"] = "x" * inside
        if low is not None and low > 0:
            roles["just_outside"] = "x" * (low - 1)
        elif high is not None:
            roles["just_outside"] = "x" * (high + 1)
        return roles

    def _enum_roles(self) -> dict[str, Any]:
        members = self.enum_values
        if not members:
            return {}
        roles: dict[str, Any] = {"min": members[0], "max": members[-1]}
        if len(members) >= 3:
            roles["just_inside"] = members[len(members) // 2]
        roles["just_outside"] = self._non_member()
        return roles

    def _non_member(self) -> str:
        candidate = "not-a-member"
        while candidate in self.enum_values:
            candidate += "-x"
        return candidate

    def _numeric_role_of(self, value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return self._position(float(value), self.minimum, self.maximum, stepped=self.type == "int")

    def _length_role_of(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return self._position(float(len(value)), self.min_length, self.max_length, stepped=True)

    @staticmethod
    def _position(value: float, low: float | None, high: float | None, *, stepped: bool) -> str | None:
        if low is None and high is None:
            return None
        if low is not None and value == low:
            return "min"
        if high is not None and value == high:
            return "max"
        outside = (low is not None and value < low) or (high is not None and value > high)
        if outside:
            if not stepped:
                return "just_outside"
            one_step = (low is not None and value == low - 1) or (high is not None and value == high + 1)
            return "just_outside" if one_step else None
        if not stepped:
            return None
        if (low is not None and value == low + 1) or (high is not None and value == high - 1):
            return "just_inside"
        return None

    def _enum_role_of(self, value: Any) -> str | None:
        members = self.enum_values
        if not members:
            return None
        if not isinstance(value, str):
            return None
        if value == members[0]:
            return "min"
        if value == members[-1]:
            return "max"
        if value in members:
            return "just_inside" if len(members) >= 3 else None
        return "just_outside"


class ErrorPath(BaseModel):
    """One documented failure the spec promises, and how to provoke it."""

    code: str = Field(min_length=1)
    condition: str
    # Inputs that, merged over a happy-path call, should produce this error.
    trigger: dict[str, Any] = Field(default_factory=dict)


class SourceRef(BaseModel):
    """Where the spec was read from, so a stale fixture is findable."""

    file: str
    symbol: str


class SpecUnderTest(BaseModel):
    """The thing tests are being written for: signature, constraints, failures."""

    id: str = Field(min_length=1)
    name: str
    kind: Literal["http_endpoint", "function"] = "http_endpoint"
    method: str | None = None
    path: str | None = None
    summary: str = ""
    params: list[ParamSpec] = Field(default_factory=list)
    errors: list[ErrorPath] = Field(default_factory=list)
    # None means the spec is silent on repetition — the axis then does not apply,
    # rather than being scored as an uncovered gap the author cannot close.
    idempotent: bool | None = None
    idempotency_note: str = ""
    expected_success: dict[str, Any] = Field(default_factory=lambda: {"status": 200})
    source: SourceRef | None = None
    # Retrieval payload: what a previous suite for this spec had to learn. This
    # is the knowledge-base half of the domain — no weights carry it.
    lessons: list[str] = Field(default_factory=list)

    def param(self, name: str) -> ParamSpec | None:
        """The declared parameter by name, or None."""
        for item in self.params:
            if item.name == name:
                return item
        return None

    def required_params(self) -> list[ParamSpec]:
        """Parameters a caller must supply."""
        return [item for item in self.params if item.required]

    def error(self, code: str) -> ErrorPath | None:
        """The documented error path by code, or None."""
        for item in self.errors:
            if item.code == code:
                return item
        return None


class TestCase(BaseModel):
    """One generated case. `boundary_role` is a claim; the scorer checks it."""

    __test__ = False

    name: str = Field(min_length=1)
    kind: TestKind
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    targets_param: str | None = None
    boundary_role: BoundaryRole | None = None
    targets_error: str | None = None
    # How many times the call is made. An idempotency case has to actually repeat.
    repeat: int = Field(default=1, ge=1)


class TestSuite(BaseModel):
    """A generated suite plus where it came from."""

    __test__ = False

    spec_id: str = Field(min_length=1)
    source: str = "deterministic"
    cases: list[TestCase] = Field(default_factory=list)
    # Recalled prior knowledge that shaped this suite, kept so the retrieval
    # contribution stays separable from the generator's own reasoning.
    notes: list[str] = Field(default_factory=list)

    def of_kind(self, kind: str) -> list[TestCase]:
        """Cases of one kind, in suite order."""
        return [case for case in self.cases if case.kind == kind]


class TestAuthoringCase(BaseModel):
    """A routable request in this domain (satisfies the RoutedCase protocol)."""

    __test__ = False

    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str]
    spec_id: str = ""
