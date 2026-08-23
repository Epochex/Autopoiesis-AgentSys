"""Small auditable operations corpus used by live investigation grounding.

This corpus supplies reference knowledge, never current-state evidence and never
action authorization.  The investigation service exposes every selected passage,
source locator, lexical score, and matched term so the UI can show exactly what
entered the model context.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

from core.memory.bm25 import tokenize
from core.memory.hybrid_kb import HybridKBRetriever, KBDocument


_DOCUMENTS: tuple[KBDocument, ...] = (
    KBDocument(
        id="systemctl-failed-units",
        text=(
            "systemd service failed unit investigation. systemctl --failed lists "
            "units in failed state. systemctl show UNIT reports ActiveState, "
            "SubState, Result and NRestarts. A failed reading is current host "
            "evidence; a historical incident is only a probe-order hint."
        ),
        metadata={
            "title": "Inspect failed systemd units",
            "source": "installed systemctl manual",
            "locator": "man:systemctl(1)#list-units-and-show",
        },
    ),
    KBDocument(
        id="systemctl-restart-contract",
        text=(
            "systemctl restart UNIT stops and starts a service. Before restart, "
            "verify that the unit is already failed and capture its current state. "
            "After restart, read ActiveState and service health again; command exit "
            "success alone does not prove sustained recovery."
        ),
        metadata={
            "title": "Restart and read back a systemd service",
            "source": "installed systemctl manual",
            "locator": "man:systemctl(1)#restart",
        },
    ),
    KBDocument(
        id="systemd-start-rate-limit",
        text=(
            "Repeated service starts are bounded by systemd StartLimitIntervalSec "
            "and StartLimitBurst. Inspect restart counters and the journal before "
            "another attempt so a restart loop does not hide a dependency failure."
        ),
        metadata={
            "title": "Bound repeated service starts",
            "source": "installed systemd.unit manual",
            "locator": "man:systemd.unit(5)#StartLimitIntervalSec",
        },
    ),
    KBDocument(
        id="systemd-service-journal",
        text=(
            "For a failed systemd service, journalctl -u UNIT shows the service log "
            "and failure result. Dependency, executable, permission and timeout "
            "errors should be checked before treating restart as a durable repair."
        ),
        metadata={
            "title": "Read the failed service journal",
            "source": "installed systemd service documentation",
            "locator": "man:systemd.service(5)#failure-result",
        },
    ),
    KBDocument(
        id="autopoiesis-dual-observation",
        text=(
            "A remediation receipt separates a fast regression window from a longer "
            "stability window. Any guard-probe regression fails quickly; success is "
            "recorded only after consecutive target and guard readings remain healthy."
        ),
        metadata={
            "title": "Dual-window remediation verification",
            "source": "Autopoiesis remediation contract",
            "locator": "docs/recurrence-contract.md",
        },
    ),
)

_ROUTING_TERMS = frozenset({
    "systemctl", "systemd", "service", "failed", "restart", "unit", "journalctl",
})


@lru_cache(maxsize=1)
def _retriever() -> HybridKBRetriever:
    # BM25 is the production-wired route on this host.  Dense and reranking
    # remain explicit optional stages and are not claimed by this receipt.
    return HybridKBRetriever(
        list(_DOCUMENTS), fusion=False, rerank=False, k=5,
    )


def retrieve_ops_knowledge(
    query: str,
    *,
    query_terms: Iterable[str] = (),
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Return scored reference passages for one live investigation."""
    if limit <= 0:
        return []
    expanded_query = " ".join([query.strip(), *[str(term) for term in query_terms if term]]).strip()
    if not expanded_query:
        return []
    retriever = _retriever()
    ranked = retriever.bm25.rank_with_scores(expanded_query, min(limit, len(_DOCUMENTS)))
    query_tokens = set(tokenize(expanded_query))
    # A single generic overlap such as "host" is not enough to route into this
    # systemd corpus.  The gate keeps unrelated network, disk, and interface
    # investigations from receiving plausible-looking but irrelevant passages.
    if not query_tokens.intersection(_ROUTING_TERMS):
        return []
    results: list[dict[str, Any]] = []
    for document_id, score in ranked:
        document = retriever.documents[document_id]
        metadata = dict(document.metadata)
        results.append({
            "document_id": document.id,
            "title": str(metadata.get("title") or document.id),
            "source": str(metadata.get("source") or "operations knowledge base"),
            "locator": str(metadata.get("locator") or document.id),
            "text": document.text,
            "route": "bm25",
            "score": float(score),
            "matched_terms": sorted(query_tokens.intersection(tokenize(document.text))),
        })
    return results


__all__ = ["retrieve_ops_knowledge"]
