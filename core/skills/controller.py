from __future__ import annotations

from core.skills.spec import RegisteredSkill


class SkillAttentionController:
    def __init__(self, enabled: bool = True, top_k: int = 3):
        self.enabled = enabled
        self.top_k = top_k

    def select(
        self,
        skills: list[RegisteredSkill],
        query_terms: list[str],
        preferred_skill_names: list[str],
    ) -> list[RegisteredSkill]:
        available = [skill for skill in skills if not skill.spec.frozen and skill.spec.risk == "read_only"]
        if not self.enabled:
            return available

        query = {term.lower() for term in query_terms}
        preferred = set(preferred_skill_names)

        def score(skill: RegisteredSkill) -> float:
            spec = skill.spec
            relevance = 5.0 if spec.name in preferred else 0.0
            tag_hits = len(query.intersection({tag.lower() for tag in spec.tags}))
            attempts = spec.success_count + spec.misuse_count
            success_rate = spec.success_count / attempts if attempts else 0.5
            misuse_rate = spec.misuse_count / attempts if attempts else 0.0
            return relevance + tag_hits + success_rate - (2.0 * misuse_rate) - (0.05 * spec.cost)

        scored = [(score(skill), skill) for skill in available]
        relevant = [(value, skill) for value, skill in scored if value > 0.5]
        return [skill for _, skill in sorted(relevant, key=lambda item: item[0], reverse=True)[: self.top_k]]
