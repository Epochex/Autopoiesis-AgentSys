from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from pydantic import BaseModel, Field

from core.memory.store import MemoryRecord


ContextStrategy = Literal["structured", "flat"]

SECTION_NAMES: tuple[str, ...] = (
    "Current State",
    "Task Spec",
    "Assets & Entities",
    "Workflow",
    "Errors & Corrections",
    "Evidence",
    "Learnings",
    "Key Results",
)

# Fractions of the content budget left after section-heading overhead. These are
# policy, not learned parameters. Largest-remainder allocation makes the integer
# budgets deterministic and ensures their sum matches the available budget.
_SECTION_WEIGHTS: dict[str, float] = {
    "Current State": 0.04,
    "Task Spec": 0.16,
    "Assets & Entities": 0.10,
    "Workflow": 0.10,
    "Errors & Corrections": 0.05,
    "Evidence": 0.35,
    "Learnings": 0.10,
    "Key Results": 0.10,
}


class ContextItemProvenance(BaseModel):
    """One atomic input and its disposition in a context section."""

    kind: str
    item_id: str | None = None
    text: str
    estimated_tokens: int
    required: bool = False
    truncated: bool = False
    reason: str | None = None


class ContextSection(BaseModel):
    """A section-local budget and complete kept/dropped provenance."""

    name: str
    token_budget: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    budget_overflow_tokens: int = 0
    kept: list[ContextItemProvenance] = Field(default_factory=list)
    dropped: list[ContextItemProvenance] = Field(default_factory=list)


class ContextPacket(BaseModel):
    """Budgeted context for one reasoning step, with full provenance."""

    case_id: str
    summary: str
    included_memory_ids: list[str] = Field(default_factory=list)
    included_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    estimated_tokens_before: int
    estimated_tokens_after: int
    compression_ratio: float
    compiler_mode: str = "flat"
    sections: list[ContextSection] = Field(default_factory=list)


@dataclass(frozen=True)
class _ContextItem:
    sequence: int
    kind: str
    item_id: str | None
    line: str
    required: bool = False
    truncated: bool = False


class ContextCompiler:
    """Compile deterministic flat or section-budgeted context.

    ``structured`` is the default. ``flat`` retains the previous line-cap and
    global-trimming algorithm for controlled comparisons. ``enabled=False``
    retains the historical ablation behavior: no caps, no trimming, and the
    original flat summary format.

    Structured memory mapping is deliberately type-based and reproducible:
    asset profiles -> Assets & Entities, procedures -> Workflow, semantic
    memories -> Learnings, and episodic outcomes -> Key Results. Current State
    and Errors & Corrections remain explicit (and may be empty) rather than
    guessing content from prose with a model or lexical classifier.
    """

    def __init__(
        self,
        token_budget: int = 900,
        enabled: bool = True,
        *,
        max_memory_lines: int = 8,
        max_evidence_lines: int = 10,
        strategy: ContextStrategy = "structured",
        section_budgets: Mapping[str, int] | None = None,
    ):
        if token_budget < 1:
            raise ValueError(f"token_budget must be >= 1, got {token_budget}")
        if max_memory_lines < 1 or max_evidence_lines < 1:
            raise ValueError("max_memory_lines and max_evidence_lines must be >= 1")
        if strategy not in ("structured", "flat"):
            raise ValueError(f"unknown context strategy: {strategy}")
        self.token_budget = token_budget
        self.enabled = enabled
        self.max_memory_lines = max_memory_lines
        self.max_evidence_lines = max_evidence_lines
        self.strategy: ContextStrategy = strategy
        self.section_budgets = self._resolve_section_budgets(section_budgets)

    def compile(
        self,
        case_id: str,
        query: str,
        memories_by_tier: dict[str, list[MemoryRecord]],
        current_evidence: list[dict],
        required_evidence: list[str],
    ) -> ContextPacket:
        """Build one packet without an LLM call or nondeterministic operation.

        Evidence items must carry an ``evidence_id``. Required evidence present
        in ``current_evidence`` is always kept, even when it exceeds a line cap,
        a section budget, or the overall token budget. Absent required ids are
        reported in ``missing_evidence``.
        """
        checked_evidence = self._checked_evidence(case_id, current_evidence)
        required = set(required_evidence)
        memory_items, evidence_items = self._build_items(memories_by_tier, checked_evidence, required)
        all_items = [self._query_item(query), *memory_items, *evidence_items]
        before = self._estimate_tokens(" ".join(item.line for item in all_items))

        if not self.enabled:
            return self._compile_flat(
                case_id,
                all_items,
                memory_items,
                evidence_items,
                required_evidence,
                before,
                apply_limits=False,
            )
        if self.strategy == "flat":
            return self._compile_flat(
                case_id,
                all_items,
                memory_items,
                evidence_items,
                required_evidence,
                before,
                apply_limits=True,
            )
        return self._compile_structured(
            case_id,
            query,
            memory_items,
            evidence_items,
            required_evidence,
            before,
        )

    def _compile_structured(
        self,
        case_id: str,
        query: str,
        memory_items: list[_ContextItem],
        evidence_items: list[_ContextItem],
        required_evidence: list[str],
        before: int,
    ) -> ContextPacket:
        by_tier = {
            tier: [item for item in memory_items if item.kind == tier]
            for tier in ("asset_profile", "procedural", "semantic", "episodic")
        }
        section_inputs: dict[str, list[_ContextItem]] = {
            "Current State": [],
            "Task Spec": [self._query_item(query)],
            "Assets & Entities": by_tier["asset_profile"],
            "Workflow": by_tier["procedural"],
            "Errors & Corrections": [],
            "Evidence": evidence_items,
            "Learnings": by_tier["semantic"],
            "Key Results": by_tier["episodic"],
        }

        sections: list[ContextSection] = []
        kept_items: list[_ContextItem] = []
        summary_parts: list[str] = []
        for name in SECTION_NAMES:
            items = section_inputs[name]
            budget = self.section_budgets[name]
            kept, dropped = self._fit_section(items, budget)
            kept_items.extend(kept)
            summary_parts.append(f"## {name}")
            summary_parts.extend(item.line for item in kept)
            after_tokens = sum(self._item_tokens(item) for item in kept)
            sections.append(
                ContextSection(
                    name=name,
                    token_budget=budget,
                    estimated_tokens_before=sum(self._item_tokens(item) for item in items),
                    estimated_tokens_after=after_tokens,
                    budget_overflow_tokens=max(0, after_tokens - budget),
                    kept=[self._provenance(item) for item in kept],
                    dropped=[self._provenance(item, reason="section_budget") for item in dropped],
                )
            )

        summary = "\n".join(summary_parts)
        return self._packet(
            case_id=case_id,
            summary=summary,
            kept_items=kept_items,
            required_evidence=required_evidence,
            before=before,
            mode="structured",
            sections=sections,
        )

    def _compile_flat(
        self,
        case_id: str,
        all_items: list[_ContextItem],
        memory_items: list[_ContextItem],
        evidence_items: list[_ContextItem],
        required_evidence: list[str],
        before: int,
        *,
        apply_limits: bool,
    ) -> ContextPacket:
        drop_reasons: dict[int, str] = {}
        if apply_limits:
            selected_memory = memory_items[: self.max_memory_lines]
            for item in memory_items[self.max_memory_lines :]:
                drop_reasons[item.sequence] = "line_cap"

            required_count = sum(item.required for item in evidence_items)
            evidence_cap = max(self.max_evidence_lines, required_count)
            selected_evidence = evidence_items[:evidence_cap]
            for item in evidence_items[evidence_cap:]:
                drop_reasons[item.sequence] = "line_cap"
        else:
            selected_memory = list(memory_items)
            selected_evidence = list(evidence_items)

        selected = [all_items[0], *selected_memory, *selected_evidence]
        while apply_limits and self._estimate_tokens(" ".join(item.line for item in selected)) > self.token_budget:
            removable_index = self._last_removable_index(selected)
            if removable_index is None:
                break
            removed = selected.pop(removable_index)
            drop_reasons[removed.sequence] = "token_budget"

        selected_sequences = {item.sequence for item in selected}
        dropped = [item for item in all_items if item.sequence not in selected_sequences]
        summary = "\n".join(item.line for item in selected)
        after = self._estimate_tokens(summary)
        section = ContextSection(
            name="Flat Context",
            token_budget=self.token_budget,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            budget_overflow_tokens=max(0, after - self.token_budget) if apply_limits else 0,
            kept=[self._provenance(item) for item in selected],
            dropped=[self._provenance(item, reason=drop_reasons[item.sequence]) for item in dropped],
        )
        return self._packet(
            case_id=case_id,
            summary=summary,
            kept_items=selected,
            required_evidence=required_evidence,
            before=before,
            mode="flat" if apply_limits else "disabled",
            sections=[section],
        )

    def _packet(
        self,
        *,
        case_id: str,
        summary: str,
        kept_items: list[_ContextItem],
        required_evidence: list[str],
        before: int,
        mode: str,
        sections: list[ContextSection],
    ) -> ContextPacket:
        after = self._estimate_tokens(summary)
        included_memory_ids = [
            item.item_id for item in kept_items if item.kind in {"asset_profile", "procedural", "semantic", "episodic"} and item.item_id
        ]
        included_evidence_ids = [
            item.item_id for item in kept_items if item.kind == "evidence" and item.item_id
        ]
        included = set(included_evidence_ids)
        missing = [evidence_id for evidence_id in required_evidence if evidence_id not in included]
        return ContextPacket(
            case_id=case_id,
            summary=summary,
            included_memory_ids=included_memory_ids,
            included_evidence_ids=included_evidence_ids,
            missing_evidence=missing,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            compression_ratio=round(after / before, 4),
            compiler_mode=mode,
            sections=sections,
        )

    def _build_items(
        self,
        memories_by_tier: dict[str, list[MemoryRecord]],
        current_evidence: list[dict],
        required: set[str],
    ) -> tuple[list[_ContextItem], list[_ContextItem]]:
        sequence = 1  # zero is reserved for the query
        memory_items: list[_ContextItem] = []
        for tier in ("asset_profile", "semantic", "procedural", "episodic"):
            for memory in memories_by_tier.get(tier, []):
                memory_items.append(
                    _ContextItem(sequence, tier, memory.memory_id, f"{tier}: {memory.text}")
                )
                sequence += 1

        evidence_items: list[_ContextItem] = []
        for item in sorted(
            current_evidence,
            key=lambda value: (value["evidence_id"] not in required, value["evidence_id"]),
        ):
            evidence_id = item["evidence_id"]
            evidence_items.append(
                _ContextItem(
                    sequence,
                    "evidence",
                    evidence_id,
                    f"{item.get('source', 'unknown')}: {item.get('summary', '')}",
                    evidence_id in required,
                )
            )
            sequence += 1
        return memory_items, evidence_items

    def _resolve_section_budgets(self, supplied: Mapping[str, int] | None) -> dict[str, int]:
        heading_tokens = sum(self._estimate_tokens(f"## {name}") for name in SECTION_NAMES)
        available = max(0, self.token_budget - heading_tokens)
        if supplied is not None:
            unknown = sorted(set(supplied) - set(SECTION_NAMES))
            missing = sorted(set(SECTION_NAMES) - set(supplied))
            if unknown or missing:
                raise ValueError(f"section_budgets must contain exactly {list(SECTION_NAMES)}")
            budgets = {name: supplied[name] for name in SECTION_NAMES}
            if any(not isinstance(value, int) or value < 0 for value in budgets.values()):
                raise ValueError("section budgets must be nonnegative integers")
            if sum(budgets.values()) > available:
                raise ValueError(
                    f"section content budgets total {sum(budgets.values())}, but only {available} tokens remain after headings"
                )
            return budgets

        raw = {name: available * _SECTION_WEIGHTS[name] for name in SECTION_NAMES}
        budgets = {name: int(raw[name]) for name in SECTION_NAMES}
        remainder = available - sum(budgets.values())
        order = sorted(SECTION_NAMES, key=lambda name: (-(raw[name] - budgets[name]), SECTION_NAMES.index(name)))
        for name in order[:remainder]:
            budgets[name] += 1
        return budgets

    @staticmethod
    def _query_item(query: str) -> _ContextItem:
        # The task itself is essential context, like required evidence. It may
        # overflow its local section rather than silently disappearing.
        return _ContextItem(0, "query", None, query, True)

    def _fit_section(
        self,
        items: list[_ContextItem],
        budget: int,
    ) -> tuple[list[_ContextItem], list[_ContextItem]]:
        kept: list[_ContextItem] = []
        dropped: list[_ContextItem] = []
        used = 0
        for item in items:
            tokens = self._item_tokens(item)
            if item.required or used + tokens <= budget:
                kept.append(item)
                used += tokens
                continue

            remaining = max(0, budget - used)
            if remaining:
                words = item.line.split()
                kept.append(
                    _ContextItem(
                        item.sequence,
                        item.kind,
                        item.item_id,
                        " ".join(words[:remaining]),
                        item.required,
                        True,
                    )
                )
                dropped.append(
                    _ContextItem(
                        item.sequence,
                        item.kind,
                        item.item_id,
                        " ".join(words[remaining:]),
                        item.required,
                        True,
                    )
                )
                used += remaining
            else:
                dropped.append(item)
        return kept, dropped

    def _provenance(self, item: _ContextItem, *, reason: str | None = None) -> ContextItemProvenance:
        return ContextItemProvenance(
            kind=item.kind,
            item_id=item.item_id,
            text=item.line,
            estimated_tokens=self._item_tokens(item),
            required=item.required,
            truncated=item.truncated,
            reason=reason,
        )

    def _item_tokens(self, item: _ContextItem) -> int:
        return self._estimate_tokens(item.line)

    @staticmethod
    def _checked_evidence(case_id: str, current_evidence: list[dict]) -> list[dict]:
        for item in current_evidence:
            if not item.get("evidence_id"):
                raise ValueError(f"case {case_id!r}: evidence item without an 'evidence_id': {sorted(item)}")
        return current_evidence

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Whitespace-token estimate: deterministic and model-agnostic; always >= 1.
        return max(1, len(text.split()))

    @staticmethod
    def _last_removable_index(selected_items: list[_ContextItem]) -> int | None:
        for index in range(len(selected_items) - 1, 0, -1):
            if not selected_items[index].required:
                return index
        return None
