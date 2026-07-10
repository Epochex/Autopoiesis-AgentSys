from __future__ import annotations

from pydantic import BaseModel, Field

from core.memory.store import MemoryRecord


class ContextPacket(BaseModel):
    """Budgeted context for one reasoning step, with full provenance of what was kept."""

    case_id: str
    summary: str
    included_memory_ids: list[str] = Field(default_factory=list)
    included_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    estimated_tokens_before: int
    estimated_tokens_after: int
    compression_ratio: float


class ContextCompiler:
    """Compress memories + evidence into a token-budgeted packet, never dropping required evidence."""

    def __init__(
        self,
        token_budget: int = 900,
        enabled: bool = True,
        *,
        max_memory_lines: int = 8,
        max_evidence_lines: int = 10,
    ):
        if token_budget < 1:
            raise ValueError(f"token_budget must be >= 1, got {token_budget}")
        if max_memory_lines < 1 or max_evidence_lines < 1:
            raise ValueError("max_memory_lines and max_evidence_lines must be >= 1")
        self.token_budget = token_budget
        self.enabled = enabled
        self.max_memory_lines = max_memory_lines
        self.max_evidence_lines = max_evidence_lines

    def compile(
        self,
        case_id: str,
        query: str,
        memories_by_tier: dict[str, list[MemoryRecord]],
        current_evidence: list[dict],
        required_evidence: list[str],
    ) -> ContextPacket:
        """Build a ContextPacket for one step.

        Evidence items must carry an `evidence_id` (raises ValueError otherwise);
        `source`/`summary` are optional. Required evidence is ordered first and is
        never evicted while trimming to `token_budget`. With `enabled=False`
        (ablation) nothing is capped or trimmed.
        """
        memory_lines: list[tuple[str, str]] = []
        for tier in ("asset_profile", "semantic", "procedural", "episodic"):
            for memory in memories_by_tier.get(tier, []):
                memory_lines.append((memory.memory_id, f"{tier}: {memory.text}"))

        required = set(required_evidence)
        evidence_lines = [
            (item["evidence_id"], f"{item.get('source', 'unknown')}: {item.get('summary', '')}")
            for item in sorted(
                self._checked_evidence(case_id, current_evidence),
                key=lambda item: (item["evidence_id"] not in required, item["evidence_id"]),
            )
        ]
        all_lines = [line for _, line in memory_lines + evidence_lines]
        before = self._estimate_tokens(query + " " + " ".join(all_lines))

        if self.enabled:
            selected_memory = memory_lines[: self.max_memory_lines]
            selected_evidence = evidence_lines[: self.max_evidence_lines]
        else:
            selected_memory = memory_lines
            selected_evidence = evidence_lines

        selected_items = [
            {"kind": "query", "id": None, "line": query},
            *[{"kind": "memory", "id": mid, "line": line} for mid, line in selected_memory],
            *[{"kind": "evidence", "id": eid, "line": line} for eid, line in selected_evidence],
        ]

        while self.enabled and self._estimate_tokens(" ".join(item["line"] for item in selected_items)) > self.token_budget and len(selected_items) > 1:
            removable_index = self._last_removable_index(selected_items, required)
            if removable_index is None:
                break
            selected_items.pop(removable_index)

        after = self._estimate_tokens(" ".join(item["line"] for item in selected_items))
        included_memory_ids = [item["id"] for item in selected_items if item["kind"] == "memory" and item["id"]]
        included_evidence_ids = [item["id"] for item in selected_items if item["kind"] == "evidence" and item["id"]]
        included = set(included_evidence_ids)
        missing = [eid for eid in required_evidence if eid not in included]

        return ContextPacket(
            case_id=case_id,
            summary="\n".join(item["line"] for item in selected_items),
            included_memory_ids=included_memory_ids,
            included_evidence_ids=included_evidence_ids,
            missing_evidence=missing,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            compression_ratio=round(after / before, 4),
        )

    @staticmethod
    def _checked_evidence(case_id: str, current_evidence: list[dict]) -> list[dict]:
        for item in current_evidence:
            if not item.get("evidence_id"):
                raise ValueError(f"case {case_id!r}: evidence item without an 'evidence_id': {sorted(item)}")
        return current_evidence

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # whitespace-token estimate: deterministic and model-agnostic; always >= 1
        return max(1, len(text.split()))

    @staticmethod
    def _last_removable_index(selected_items: list[dict], required: set[str]) -> int | None:
        for index in range(len(selected_items) - 1, 0, -1):
            item = selected_items[index]
            if item["kind"] == "evidence" and item["id"] in required:
                continue
            return index
        return None
