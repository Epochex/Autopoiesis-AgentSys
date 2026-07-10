from __future__ import annotations

from core.skills.spec import RegisteredSkill


class SkillAttentionController:
    """Selects which read-only skills a run may see (the attention gate over the skill library).

    Relevance is a hard gate; learned success/misuse rates only rank inside the
    relevant set so a globally "good" skill can never widen its own scope.
    """

    def __init__(self, enabled: bool = True, top_k: int = 3):
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        self.enabled = enabled
        self.top_k = top_k

    def select(
        self,
        skills: list[RegisteredSkill],
        query_terms: list[str],
        preferred_skill_names: list[str],
    ) -> list[RegisteredSkill]:
        """Return at most `top_k` unfrozen read-only skills relevant to `query_terms`.

        With `enabled=False` (ablation) every unfrozen read-only skill is returned.
        """
        available = [skill for skill in skills if not skill.spec.frozen and skill.spec.risk == "read_only"]
        if not self.enabled:
            return available

        query = {term.lower() for term in query_terms}
        preferred = set(preferred_skill_names)

        def topical_relevance(skill: RegisteredSkill) -> float:
            # a skill is only *eligible* if it is topically relevant to this query
            # (preferred or tag-matched) — a globally high success_rate must never pull
            # an off-topic skill into scope. Learning ranks the relevant set; it can't widen it.
            preferred_hit = 5.0 if skill.spec.name in preferred else 0.0
            tag_hits = len(query.intersection({tag.lower() for tag in skill.spec.tags}))
            return preferred_hit + tag_hits

        def score(skill: RegisteredSkill) -> float:
            spec = skill.spec
            attempts = spec.success_count + spec.misuse_count
            success_rate = spec.success_count / attempts if attempts else 0.5
            misuse_rate = spec.misuse_count / attempts if attempts else 0.0
            return topical_relevance(skill) + success_rate - (2.0 * misuse_rate) - (0.05 * spec.cost)

        candidates = [(score(skill), skill) for skill in available if topical_relevance(skill) > 0.0]
        return [skill for _, skill in sorted(candidates, key=lambda item: item[0], reverse=True)[: self.top_k]]
