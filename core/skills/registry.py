from __future__ import annotations

from core.skills.spec import RegisteredSkill, SkillResult, SkillSpec


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, RegisteredSkill] = {}

    def register(self, spec: SkillSpec, handler) -> None:
        self._skills[spec.name] = RegisteredSkill(spec=spec, handler=handler)

    def get(self, name: str) -> RegisteredSkill:
        return self._skills[name]

    def all(self) -> list[RegisteredSkill]:
        return list(self._skills.values())

    def execute(self, name: str, **kwargs) -> SkillResult:
        skill = self.get(name)
        return skill.handler(**kwargs)
