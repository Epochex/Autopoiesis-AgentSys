from __future__ import annotations

from collections.abc import Callable

from core.skills.spec import RegisteredSkill, SkillResult, SkillSpec


class SkillRegistry:
    """In-memory skill catalogue keyed by unique skill name."""

    def __init__(self) -> None:
        self._skills: dict[str, RegisteredSkill] = {}

    def register(self, spec: SkillSpec, handler: Callable[..., SkillResult], *, replace: bool = False) -> None:
        """Register `handler` under `spec.name`.

        Raises ValueError on a duplicate name unless `replace=True` — silently
        clobbering an existing capability is never a valid promotion path.
        """
        if not replace and spec.name in self._skills:
            raise ValueError(f"skill already registered: {spec.name!r} (pass replace=True to overwrite)")
        self._skills[spec.name] = RegisteredSkill(spec=spec, handler=handler)

    def get(self, name: str) -> RegisteredSkill:
        """Return the registered skill; raises KeyError for an unknown name."""
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name!r}") from None

    def all(self) -> list[RegisteredSkill]:
        """Return all registered skills in registration order."""
        return list(self._skills.values())

    def execute(self, name: str, **kwargs) -> SkillResult:
        """Execute the named skill; raises TypeError if the handler breaks the SkillResult contract."""
        skill = self.get(name)
        result = skill.handler(**kwargs)
        if not isinstance(result, SkillResult):
            raise TypeError(
                f"skill {name!r} handler returned {type(result).__name__}, expected SkillResult"
            )
        return result
