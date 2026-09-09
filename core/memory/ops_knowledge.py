"""Small auditable operations corpus used by live investigation grounding.

This corpus supplies reference knowledge, never current-state evidence and never
action authorization.  The investigation service exposes every selected passage,
source locator, lexical score, and matched term so the UI can show exactly what
entered the model context.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
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


def _configured_corpus_path() -> Path | None:
    value = os.environ.get("AUTOPOIESIS_KNOWLEDGE_CORPUS_PATH") or os.environ.get(
        "SELFEVO_KNOWLEDGE_CORPUS_PATH"
    )
    if value:
        return Path(value).expanduser().resolve()
    from core.memory.knowledge_sync import published_corpus_path

    automatic = published_corpus_path()
    return automatic if automatic.is_file() else None


def _corpus_source(path: Path | None) -> str:
    configured = os.environ.get("AUTOPOIESIS_KNOWLEDGE_CORPUS_PATH") or os.environ.get(
        "SELFEVO_KNOWLEDGE_CORPUS_PATH"
    )
    if configured:
        return "configured_file"
    return "device_auto_sync" if path is not None else "builtin_fallback"


def _dense_requested(mode: str) -> bool:
    if mode == "hybrid":
        return True
    value = os.environ.get("AUTOPOIESIS_KNOWLEDGE_DENSE_ENABLED", "0")
    return mode == "auto" and value.strip().lower() in {"1", "true", "yes", "on"}


def _retrieval_mode() -> str:
    value = (
        os.environ.get("AUTOPOIESIS_KNOWLEDGE_RETRIEVAL_MODE")
        or os.environ.get("SELFEVO_KNOWLEDGE_RETRIEVAL_MODE")
        or "auto"
    ).strip().lower()
    if value not in {"auto", "bm25", "hybrid"}:
        raise ValueError("KNOWLEDGE_RETRIEVAL_MODE must be auto, bm25, or hybrid")
    return value


@lru_cache(maxsize=8)
def _external_retriever(
    path_text: str,
    modified_ns: int,
    mode: str,
) -> tuple[HybridKBRetriever, str, str | None]:
    del modified_ns  # cache-key input; the file content is read below
    path = Path(path_text)
    corpus = json.loads(path.read_text(encoding="utf-8"))
    use_dense = _dense_requested(mode)
    if use_dense:
        try:
            retriever = HybridKBRetriever.from_corpus(
                    corpus,
                    fusion=True,
                    rerank=False,
                    k=8,
                    dense_cache_key=f"live-kb:{path}:{path.stat().st_mtime_ns}",
                )
            index_type = str(getattr(retriever.dense_index, "index_type", "unknown"))
            return (
                retriever,
                f"bm25+bge+faiss-{index_type}+rrf",
                None,
            )
        except Exception as exc:
            if mode == "hybrid":
                raise
            degraded = f"{type(exc).__name__}: {exc}"[:240]
    else:
        degraded = None
    return (
        HybridKBRetriever.from_corpus(corpus, fusion=False, rerank=False, k=8),
        "bm25",
        degraded,
    )


@lru_cache(maxsize=1)
def _builtin_retriever() -> HybridKBRetriever:
    # BM25 is the production-wired route on this host.  Dense and reranking
    # remain explicit optional stages and are not claimed by this receipt.
    return HybridKBRetriever(
        list(_DOCUMENTS), fusion=False, rerank=False, k=5,
    )


def _retriever() -> tuple[HybridKBRetriever, str, str | None, bool]:
    path = _configured_corpus_path()
    if path is None:
        return _builtin_retriever(), "bm25", None, True
    if not path.is_file():
        return _builtin_retriever(), "bm25", f"corpus_not_found:{path}", True
    retriever, route, degraded = _external_retriever(
        str(path), path.stat().st_mtime_ns, _retrieval_mode()
    )
    return retriever, route, degraded, False


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
    retriever, route, degraded_reason, builtin = _retriever()
    query_tokens = set(tokenize(expanded_query))
    # A single generic overlap such as "host" is not enough to route into this
    # systemd corpus.  The gate keeps unrelated network, disk, and interface
    # investigations from receiving plausible-looking but irrelevant passages.
    if builtin and not query_tokens.intersection(_ROUTING_TERMS):
        return []
    if route == "bm25":
        scored = retriever.bm25.rank_with_scores(
            expanded_query, min(limit, len(retriever))
        )
        ranked = [(retriever.documents[document_id], float(score)) for document_id, score in scored]
    else:
        documents = retriever.retrieve(expanded_query, min(limit, len(retriever)))
        ranked = [(document, 1.0 / rank) for rank, document in enumerate(documents, start=1)]
    results: list[dict[str, Any]] = []
    for document, score in ranked:
        metadata = dict(document.metadata)
        results.append({
            "document_id": document.id,
            "title": str(metadata.get("title") or metadata.get("section_title") or document.id),
            "source": str(metadata.get("source") or "operations knowledge base"),
            "locator": str(metadata.get("locator") or document.id),
            "text": document.text,
            "route": route,
            "score": float(score),
            "matched_terms": sorted(query_tokens.intersection(tokenize(document.text))),
            "degraded_reason": degraded_reason,
            "applicable_versions": list(metadata.get("applicable_versions") or ()),
            "asset_ids": list(metadata.get("asset_ids") or ()),
            "entity_ids": list(metadata.get("entity_ids") or ()),
            "product": metadata.get("product"),
            "document_type": metadata.get("document_type"),
        })
    return results


def knowledge_backend_status() -> dict[str, Any]:
    """Return the exact corpus and retrieval route used by live investigation."""
    retriever, route, degraded_reason, builtin = _retriever()
    path = _configured_corpus_path()
    from core.memory.knowledge_sync import knowledge_sync_status

    return {
        "corpus": "builtin-systemd" if builtin else str(path),
        "corpus_source": _corpus_source(path),
        "external_configured": _corpus_source(path) == "configured_file",
        "document_count": len(retriever),
        "route": route,
        "dense_enabled": route != "bm25",
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "automatic_sync": knowledge_sync_status(),
    }


__all__ = ["knowledge_backend_status", "retrieve_ops_knowledge"]
