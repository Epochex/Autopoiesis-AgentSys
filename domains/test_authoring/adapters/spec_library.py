from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domains.test_authoring.schema import SpecUnderTest


class SpecLibrary:
    """Fixture-backed library of specs under test; no external I/O.

    Doubles as the retrieval corpus. A case is bound to exactly one spec, and
    `snapshot` hands out a fresh copy each time so a skill cannot mutate the
    library through the state it was given.
    """

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        self._specs: dict[str, SpecUnderTest] = {}
        for item in payload["specs"]:
            spec = SpecUnderTest.model_validate(item)
            if spec.id in self._specs:
                raise ValueError(f"duplicate spec id in {self.fixture_path}: {spec.id!r}")
            # An example that its own constraints reject poisons every happy-path
            # case built from it, and the resulting low score looks like a
            # generator fault rather than a fixture typo.
            for param in spec.params:
                if param.example is not None and not param.satisfies(param.example):
                    raise ValueError(
                        f"spec {spec.id!r} gives {param.name!r} an example its own constraints reject"
                    )
            self._specs[spec.id] = spec
        self._bindings: dict[str, str] = {}

    @classmethod
    def from_path(cls, path: str | Path) -> "SpecLibrary":
        return cls(path)

    def bind(self, case_id: str, spec_id: str) -> None:
        """Point a case at the spec it is about; raises KeyError for an unknown spec."""
        if spec_id not in self._specs:
            raise KeyError(f"unknown spec: {spec_id!r}")
        self._bindings[case_id] = spec_id

    def has_case(self, case_id: str) -> bool:
        """True when a spec is bound to `case_id`."""
        return case_id in self._bindings

    def spec_for(self, case_id: str) -> SpecUnderTest:
        """The spec bound to `case_id`; raises KeyError when nothing is bound."""
        try:
            return self._specs[self._bindings[case_id]]
        except KeyError:
            raise KeyError(f"no spec bound to case: {case_id!r}") from None

    def snapshot(self, case_id: str) -> dict[str, Any]:
        """Observable state for contract checking: the bound spec, as plain data."""
        return self.spec_for(case_id).model_dump()

    def specs(self) -> list[SpecUnderTest]:
        """Every spec in the library, in fixture order."""
        return list(self._specs.values())

    def spec(self, spec_id: str) -> SpecUnderTest:
        """One spec by id; raises KeyError when it is not in the library."""
        try:
            return self._specs[spec_id]
        except KeyError:
            raise KeyError(f"unknown spec: {spec_id!r}") from None

    def corpus_for(self, spec_id: str) -> list[SpecUnderTest]:
        """Everything a recall pass may look at — every spec except this one."""
        return [spec for spec in self._specs.values() if spec.id != spec_id]
