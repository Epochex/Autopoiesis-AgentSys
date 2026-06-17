from __future__ import annotations

from pydantic import BaseModel, Field

from core.memory.store import MemoryRecord


class ContextPacket(BaseModel):
    case_id: str
    summary: str
    included_memory_ids: list[str] = Field(default_factory=list)
    included_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    estimated_tokens_before: int
    estimated_tokens_after: int
    compression_ratio: float


class ContextCompiler:
    def __init__(self, token_budget: int = 900, enabled: bool = True):
        self.token_budget = token_budget
        self.enabled = enabled

    def compile(
        self,
        case_id: str,
        query: str,
        memories_by_tier: dict[str, list[MemoryRecord]],
        current_evidence: list[dict],
        required_evidence: list[str],
    ) -> ContextPacket:
        memory_lines: list[tuple[str, str]] = []
        for tier in ("asset_profile", "semantic", "procedural", "episodic"):
            for memory in memories_by_tier.get(tier, []):
                memory_lines.append((memory.memory_id, f"{tier}: {memory.text}"))

        evidence_lines = [
            (item["evidence_id"], f"{item['source']}: {item['summary']}")
            for item in current_evidence
        ]
        all_lines = [line for _, line in memory_lines + evidence_lines]
        before = self._estimate_tokens(query + " " + " ".join(all_lines))

        if self.enabled:
            selected_memory = memory_lines[:8]
            selected_evidence = evidence_lines[:10]
        else:
            selected_memory = memory_lines
            selected_evidence = evidence_lines

        selected_items = [
            {"kind": "query", "id": None, "line": query},
            *[{"kind": "memory", "id": mid, "line": line} for mid, line in selected_memory],
            *[{"kind": "evidence", "id": eid, "line": line} for eid, line in selected_evidence],
        ]

        while self.enabled and self._estimate_tokens(" ".join(item["line"] for item in selected_items)) > self.token_budget and len(selected_items) > 1:
            selected_items.pop(-1)

        after = self._estimate_tokens(" ".join(item["line"] for item in selected_items))
        if before <= 0:
            compression = 1.0
        else:
            compression = round(after / before, 4)
        included_memory_ids = [item["id"] for item in selected_items if item["kind"] == "memory" and item["id"]]
        included_evidence_ids = [item["id"] for item in selected_items if item["kind"] == "evidence" and item["id"]]
        missing = [eid for eid in required_evidence if eid not in set(included_evidence_ids)]

        return ContextPacket(
            case_id=case_id,
            summary="\n".join(item["line"] for item in selected_items),
            included_memory_ids=included_memory_ids,
            included_evidence_ids=included_evidence_ids,
            missing_evidence=missing,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            compression_ratio=compression,
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text.split()))
