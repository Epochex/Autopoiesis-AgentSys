"""Locate a contaminating memory with reversible counterfactual probes.

Memory contamination can present exactly like a model-alignment failure.  This
module removes one active memory at a time, reruns the caller's reproducer, and
reports the first removal that makes the anomaly disappear.  The decision stays
with the caller-provided boolean signal; no model judgment is added here.

Every removal is temporary.  The complete record snapshot is restored after
each probe, including when the reproducer raises, because quarantine is the
store's supported withdrawal path and memory history must not be deleted.
"""
from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from core.memory.store import TieredMemoryStore


class BisectResult(BaseModel):
    """JSON-ready outcome and evidence from a memory contamination scan."""

    culprit_id: str | None
    probe_count: int
    probes: list[tuple[str, bool]] = Field(default_factory=list)


def bisect(
    store: TieredMemoryStore,
    reproduce: Callable[[], bool],
    *,
    candidates: list[str] | None = None,
    max_probes: int = 32,
) -> BisectResult:
    """Return the first memory whose temporary removal clears the anomaly.

    By default, active memories are checked one at a time in descending
    ``access_count`` order.  Equal counts retain store insertion order.  An
    explicit candidate list is used in the order supplied.
    """
    if isinstance(max_probes, bool) or not isinstance(max_probes, int):
        raise TypeError("max_probes must be an integer")
    if max_probes < 0:
        raise ValueError("max_probes must be non-negative")

    active = store.active()
    active_ids = {record.memory_id for record in active}
    if candidates is None:
        candidate_ids = [
            record.memory_id
            for record in sorted(
                active,
                key=lambda record: record.access_count,
                reverse=True,
            )
        ]
    else:
        candidate_ids = list(candidates)
        unavailable = [memory_id for memory_id in candidate_ids if memory_id not in active_ids]
        if unavailable:
            joined = ", ".join(unavailable)
            raise ValueError(f"candidates are not active memories: {joined}")

    # A full snapshot removes both the temporary flag and its quarantine tag.
    # Reusing it after every probe also makes all probes start from the same state.
    snapshot = [record.model_copy(deep=True) for record in store.records()]
    probes: list[tuple[str, bool]] = []

    for memory_id in candidate_ids[:max_probes]:
        try:
            store.quarantine(memory_id, "bisect_probe")
            anomaly_present = bool(reproduce())
        finally:
            store.replace_records(snapshot)

        probes.append((memory_id, anomaly_present))
        if not anomaly_present:
            return BisectResult(
                culprit_id=memory_id,
                probe_count=len(probes),
                probes=probes,
            )

    return BisectResult(culprit_id=None, probe_count=len(probes), probes=probes)
