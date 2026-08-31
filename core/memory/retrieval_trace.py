"""Scenario-aware, per-stage trace of the project's REAL retrieval flows, for the UI.

``build_retrieval_trace`` returns a ``cases`` array of REAL worked examples shaped
exactly for the ``/api/rca/retrieval`` frontend page.  Different cases genuinely run
different retrieval flows in this codebase, and the trace makes that difference
visible with REAL queries, REAL document text, REAL match reasons, REAL module output
and REAL trigger counts — nothing is hand-authored and no self-Q&A query or invented
number is introduced.  Every hit carries the referenced document's real body ``text``
and the REAL ``matched`` terms/assets (a genuine token/asset intersection) that
surfaced it:

* several ``memory_recall`` cases — the flagship network-RCA scenario's flow.  Each
  is a DIFFERENT real network-RCA case (from ``domains/network_rca/seed_cases``); for
  each, its real case query is run through the genuine tiered memory store
  (:class:`core.memory.store.TieredMemoryStore`, seeded from the domain's
  ``memory_seed.json``) exactly as :meth:`core.orchestrator.orchestrator.
  SingleAgentRCAOrchestrator._run` calls it — ``limit_per_tier=6``, ``graph_depth=2``.
  Mechanism = Okapi BM25 lexical overlap + exact-asset identity, an optional dense
  route (omitted honestly here — no dense index is attached), and a bounded
  structural graph-hop expansion.  The reconstructed tier hits match the persisted
  ``memory_read`` payloads in ``artifacts/network_rca_phase1_trace.jsonl`` (5 cases),
  which is the honest trigger count.  This flow uses NO fusion/gate/rerank — that
  absence is the point.

* ``kb_hybrid`` (flow ``kb_hybrid``) — the corpus/KB flow (HybridKBRetriever), used
  only for KB questions and by the eval harnesses, never by the live network-RCA
  scenario; its trigger count is honestly ``live: false`` (eval-only).  Its every
  score/rank/verdict is the actual output of the underlying module:

* first-stage routes -----------------------------------------------------------
    - ``bm25``    : :class:`core.memory.bm25.BM25Index` (Okapi BM25 sparse lexical);
    - ``dense``   : :class:`core.eval.dense_retrieval.DenseIndex` (bge/faiss vectors)
                    — OPTIONAL; honestly reported ``enabled: false`` with the real
                    ImportError note when the ``dense`` extra is not installed;
    - ``logical`` : curated-tag structured overlap, the same structured retriever
                    used by :mod:`core.eval.skill_retrieval` (``kind: structured``).
* fusion  : :func:`core.memory.rrf.rrf_fuse` (Reciprocal Rank Fusion, c=60).  The
            reported ``rrf_score`` is recomputed with the module's own ``1/(c+rank)``
            formula over the exact rankings fed to ``rrf_fuse``, and the ordering is
            asserted identical to ``rrf_fuse``'s output so the numbers cannot drift.
* gate    : :func:`core.memory.crag_gate.crag_gate` (CRAG-style confidence gate)
            over the BM25 confidence signal — real correct/ambiguous/incorrect.
* rerank  : :class:`core.eval.reranker.CrossEncoderReranker` — OPTIONAL; disabled
            with an honest note when the ``rerank`` extra is absent.  ``delta`` is
            computed from real fusion-rank vs rerank-rank.
* context : :class:`core.context.compiler.ContextCompiler` budgets the gate-approved
            passages; token counts come from the compiler's own tokenizer.

The corpus is a small, fixed set of authored FortiOS/FortiGate operations-knowledge
passages (the domain this project targets).  It is demo knowledge text — labelled
honestly as such in ``corpus.source`` — but every retrieval number computed over it
is a genuine module result, which is the honesty guarantee that matters here.

Query expansion (:mod:`core.memory.query_expansion`) is applied symmetrically
(stemmed documents, stem+synonym query) exactly as in the skill-retrieval eval, and
the added expansion terms are surfaced verbatim in ``query.expansions``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from core.context.compiler import ContextCompiler
from core.memory.bm25 import BM25Index, tokenize
from core.memory.crag_gate import crag_gate
from core.memory.hybrid_kb import DEFAULT_DENSE_MODEL, DEFAULT_RERANKER_MODEL
from core.memory.query_expansion import make_transform
from core.memory.rrf import rrf_fuse
from core.memory.store import MemoryRecord, TieredMemoryStore

# ── fixed, auditable demo corpus: FortiOS/FortiGate ops-knowledge passages ──────
# Each record: id, title, text (device/product vocabulary), curated structured tags,
# and an honest source label.  Kept small (~15) and fixed so the trace is stable.
_CORPUS: tuple[dict[str, Any], ...] = (
    {
        "id": "fos-admin-lockout",
        "title": "Administrator login lockout after failed attempts",
        "text": (
            "FortiGate locks out an administrator account after repeated failed login "
            "attempts. The admin-lockout-threshold sets how many failed attempts are "
            "allowed before the account is blocked, and admin-lockout-duration sets how "
            "long the lockout lasts. Failed admin logins raise the 'Admin login failed' "
            "event with reason name_invalid or passwd_invalid."
        ),
        "tags": ["admin", "login", "lockout", "authentication", "failed", "threshold"],
        "source": "FortiOS admin auth ops note",
    },
    {
        "id": "fos-admin-trusthost",
        "title": "Restricting administrative access with trusted hosts",
        "text": (
            "Restrict administrator access by configuring trusted hosts so management "
            "logins are only accepted from approved management subnets. Combine trusted "
            "hosts with limiting admin access to the management interface to reduce the "
            "exposed administrative attack surface."
        ),
        "tags": ["admin", "trusthost", "management", "access", "hardening"],
        "source": "FortiOS admin auth ops note",
    },
    {
        "id": "fos-ldap-auth",
        "title": "Configuring an LDAP server for authentication",
        "text": (
            "FortiOS can use a remote LDAP server for user authentication. Configure the "
            "LDAP server connection under User and Authentication, set the bind type, and "
            "reference the server in a firewall authentication policy so users are "
            "authenticated before traffic is allowed."
        ),
        "tags": ["ldap", "authentication", "user", "remote-server"],
        "source": "FortiOS user auth ops note",
    },
    {
        "id": "fos-policy-deny",
        "title": "Implicit deny and firewall policy ordering",
        "text": (
            "Traffic that matches no explicit firewall policy is dropped by the implicit "
            "deny policy and logged with action deny. Policy order matters: policies are "
            "evaluated top to bottom and the first match wins, so a broad deny above a "
            "specific allow will block the intended traffic."
        ),
        "tags": ["policy", "deny", "firewall", "implicit-deny", "traffic"],
        "source": "FortiOS policy ops note",
    },
    {
        "id": "fos-policy-log",
        "title": "Logging denied traffic for policy troubleshooting",
        "text": (
            "Enable logging on firewall policies to record denied sessions. Denied traffic "
            "appears in the forward traffic log with action deny and a policyid, which is "
            "how a blocked application or port probe is traced back to the policy that "
            "dropped the flow."
        ),
        "tags": ["policy", "deny", "logging", "traffic", "troubleshooting"],
        "source": "FortiOS logging ops note",
    },
    {
        "id": "fos-ips-signature",
        "title": "IPS signatures and intrusion prevention profiles",
        "text": (
            "The intrusion prevention system matches traffic against IPS signatures. Apply "
            "an IPS profile to a firewall policy to block known exploits; signature actions "
            "can be set to block, pass, or monitor per signature or severity."
        ),
        "tags": ["ips", "intrusion-prevention", "signature", "exploit", "profile"],
        "source": "FortiOS security-profile ops note",
    },
    {
        "id": "fos-ssl-vpn",
        "title": "SSL VPN portal and web mode access",
        "text": (
            "SSL VPN gives remote users access through a browser portal or tunnel mode. "
            "Configure the SSL VPN portal, assign it to a user group, and create a firewall "
            "policy from the ssl.root interface to the internal network to authorize access."
        ),
        "tags": ["ssl-vpn", "remote-access", "portal", "vpn"],
        "source": "FortiOS VPN ops note",
    },
    {
        "id": "fos-ipsec-vpn",
        "title": "Site-to-site IPsec VPN phase 1 and phase 2",
        "text": (
            "A site-to-site IPsec VPN is built from a phase 1 IKE negotiation and one or "
            "more phase 2 selectors. Mismatched pre-shared keys or phase 2 selectors are the "
            "most common cause of a tunnel that negotiates phase 1 but never brings up "
            "phase 2."
        ),
        "tags": ["ipsec", "vpn", "phase1", "phase2", "tunnel"],
        "source": "FortiOS VPN ops note",
    },
    {
        "id": "fos-ha-cluster",
        "title": "FortiGate HA active-passive failover",
        "text": (
            "High availability clusters two or more FortiGate units. In active-passive mode "
            "the primary handles traffic and the secondary takes over on failover. Heartbeat "
            "interface failure or a device-priority change can trigger an unexpected "
            "failover and a brief session clash."
        ),
        "tags": ["ha", "cluster", "failover", "active-passive", "heartbeat"],
        "source": "FortiOS HA ops note",
    },
    {
        "id": "fos-dhcp-server",
        "title": "Configuring a DHCP server on an interface",
        "text": (
            "Enable a DHCP server on an internal interface to lease addresses to clients. "
            "Set the address range, netmask, default gateway, and DNS. An exhausted address "
            "pool or an overlapping range is a common cause of clients failing to obtain a "
            "lease."
        ),
        "tags": ["dhcp", "server", "lease", "interface", "addressing"],
        "source": "FortiOS network ops note",
    },
    {
        "id": "fos-dns-filter",
        "title": "DNS filter profile and web rating",
        "text": (
            "A DNS filter profile blocks or monitors domains by category before a full web "
            "filter is applied. Apply the DNS filter to a firewall policy to stop clients "
            "resolving known malicious domains."
        ),
        "tags": ["dns", "filter", "web-rating", "category", "profile"],
        "source": "FortiOS security-profile ops note",
    },
    {
        "id": "fos-fortiview-sessions",
        "title": "FortiView sessions and top talkers",
        "text": (
            "FortiView visualizes live and historical sessions, showing top sources, "
            "destinations, and applications. Use FortiView to identify a host generating an "
            "abnormally high volume of sessions or flows before deciding whether to deny it."
        ),
        "tags": ["fortiview", "sessions", "monitoring", "top-talkers", "flows"],
        "source": "FortiOS monitoring ops note",
    },
    {
        "id": "fos-log-disk",
        "title": "Local disk logging and log retention",
        "text": (
            "FortiGate can store logs on local disk when a FortiAnalyzer is not available. "
            "Disk logging has limited retention and can wear the disk, so high-volume event "
            "and traffic logs are usually forwarded to a syslog or FortiAnalyzer collector."
        ),
        "tags": ["logging", "disk", "retention", "syslog", "fortianalyzer"],
        "source": "FortiOS logging ops note",
    },
    {
        "id": "fos-cpu-conserve",
        "title": "High CPU and conserve mode",
        "text": (
            "Sustained high CPU or memory pressure pushes FortiGate into conserve mode, "
            "where new sessions may be dropped to protect the device. Runaway proxy or IPS "
            "processes and a session-table flood are typical triggers."
        ),
        "tags": ["cpu", "memory", "conserve-mode", "performance", "processor"],
        "source": "FortiOS performance ops note",
    },
    {
        "id": "fos-snat-pool",
        "title": "Source NAT and IP pools",
        "text": (
            "Outbound traffic is source-NATed to the egress interface address by default. "
            "Use an IP pool to translate to a different address or a range for outbound "
            "sessions, which is required when a downstream service expects a fixed source "
            "address."
        ),
        "tags": ["nat", "snat", "ip-pool", "outbound", "translation"],
        "source": "FortiOS network ops note",
    },
)

_CORPUS_SOURCE = "authored FortiOS/FortiGate ops-knowledge passages (fixed demo corpus)"

# Default query: an admin login-lockout question — a realistic FortiGate admin-auth
# incident that aligns with the admin_login_failed / admin-bruteforce-lockout cases
# in the project's FortiGate/FortiOS retrieval evals.
_DEFAULT_QUERY = "administrator login lockout after repeated failed login attempts"

# CRAG thresholds over the BM25 confidence signal.  Documented policy, not tuning:
# hi is cleared by a passage that matches several distinct query terms (a confident
# lexical ground), lo floors an incidental single-term brush; between them the gate
# widens the pool.  Same shape as core.eval.fortigate_corpus_retrieval.demo_crag_gate.
_GATE_HI = 3.0
_GATE_LO = 0.5

_FUSION_K = 10          # fused ordering length reported to the UI
_ROUTE_DEPTH = 12       # per-route depth fed into fusion / rerank
_CONTEXT_BUDGET = 240   # token budget for the context-compiler stage

_ROUTE_LABELS: dict[str, dict[str, str]] = {
    "bm25": {"zh": "BM25 稀疏词法", "en": "BM25 sparse lexical"},
    "dense": {"zh": "稠密向量 (bge/faiss)", "en": "Dense vector (bge/faiss)"},
    "logical": {"zh": "结构化标签检索", "en": "Structured tag retrieval"},
}

_GATE_ACTIONS: dict[str, dict[str, str]] = {
    "correct": {
        "zh": "高置信：直接采用检索结果",
        "en": "High confidence: use retrieved results as-is",
    },
    "ambiguous": {
        "zh": "歧义：扩大候选池后返回更多",
        "en": "Ambiguous: widen the candidate pool and return more",
    },
    "incorrect": {
        "zh": "低置信：放弃检索并降级为未观测",
        "en": "Low confidence: abstain (degrade to not-observed)",
    },
}


def _snippet(text: str, limit: int = 150) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _extra_expansions(base_tokens: list[str], expanded_tokens: list[str]) -> list[str]:
    """Expansion terms the query gained over its plain tokenization, order-stable."""
    seen = set(base_tokens)
    out: list[str] = []
    for tok in expanded_tokens:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _bm25_hits(
    scored: list[tuple[str, float]], matched_by_id: dict[str, list[str]],
) -> list[dict[str, Any]]:
    hits = []
    for rank, (doc_id, score) in enumerate(scored, start=1):
        doc = _DOCS[doc_id]
        hits.append({
            "doc_id": doc_id,
            "rank": rank,
            "score": float(score),
            "title": doc["title"],
            "snippet": _snippet(doc["text"]),
            "text": doc["text"],
            "matched": matched_by_id.get(doc_id, []),
        })
    return hits


def _structured_scored(tags_by_id: dict[str, list[str]], qterms: set[str]) -> list[tuple[str, float]]:
    """Curated-tag overlap count per doc — the structured retriever's raw signal.

    Mirrors :func:`core.eval.skill_retrieval._structured_retriever`: a doc scores by
    how many of its curated tags appear in the (expanded) query terms; zero-overlap
    docs are dropped so the route can abstain.  Deterministic, ties break on id.
    """
    scored = []
    for doc_id in sorted(tags_by_id):
        hits = sum(1 for tag in tags_by_id[doc_id] if tag in qterms)
        if hits:
            scored.append((doc_id, float(hits)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _route_hits(
    scored: list[tuple[str, float]],
    matched_by_id: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    matched_by_id = matched_by_id or {}
    hits = []
    for rank, (doc_id, score) in enumerate(scored, start=1):
        doc = _DOCS[doc_id]
        hits.append({
            "doc_id": doc_id,
            "rank": rank,
            "score": float(score),
            "title": doc["title"],
            "snippet": _snippet(doc["text"]),
            "text": doc["text"],
            "matched": matched_by_id.get(doc_id, []),
        })
    return hits


def _fuse_with_scores(
    rankings: dict[str, list[str]], k: int, c: int = 60,
) -> list[dict[str, Any]]:
    """RRF fusion that also reports per-doc score and contributing routes.

    Scores use the module's own ``1/(c+rank)`` formula; the resulting id ordering is
    asserted identical to :func:`core.memory.rrf.rrf_fuse` so the reported numbers can
    never diverge from the canonical fusion.
    """
    scores: dict[str, float] = {}
    from_routes: dict[str, list[str]] = {}
    for route_id, ranking in rankings.items():
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (c + rank)
            from_routes.setdefault(doc_id, []).append(route_id)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]

    canonical = rrf_fuse(list(rankings.values()), k, c=c)
    assert [doc_id for doc_id, _ in ordered] == canonical, "RRF score ordering drifted from rrf_fuse"

    out = []
    for rank, (doc_id, score) in enumerate(ordered, start=1):
        out.append({
            "doc_id": doc_id,
            "rank": rank,
            "rrf_score": round(score, 6),
            "from_routes": from_routes[doc_id],
            "title": _DOCS[doc_id]["title"],
        })
    return out


def _dense_route(doc_ids: list[str], texts: list[str], query: str, depth: int) -> dict[str, Any]:
    """Run the real dense route, or honestly report why it is unavailable.

    The heavy embedding/index stack is optional; when the ``dense`` extra is missing
    the route degrades to ``enabled: false`` with the real ImportError text rather
    than failing the whole trace.
    """
    route: dict[str, Any] = {
        "id": "dense",
        "label": _ROUTE_LABELS["dense"],
        "kind": "vector",
        "enabled": False,
        "note": None,
        "hits": [],
    }
    try:
        from core.eval.dense_retrieval import DenseIndex

        index = DenseIndex.build(doc_ids, texts, model_name=DEFAULT_DENSE_MODEL, index_type="flat")
        rows = index.search_texts([query], depth, model_name=DEFAULT_DENSE_MODEL)
        route["enabled"] = True
        route["hits"] = _route_hits([(doc_id, round(score, 6)) for doc_id, score in (rows[0] if rows else [])])
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, report honestly
        route["note"] = (
            "dense index requires the `dense` extra "
            "(sentence-transformers + faiss-cpu + torch): "
            f"{type(exc).__name__}: {exc}"
        )
    return route


def _rerank_stage(query: str, fused: list[dict[str, Any]], depth: int) -> dict[str, Any]:
    """Cross-encoder rerank of the fused pool, or an honest disabled stage.

    ``delta`` is real: fusion rank minus rerank rank (positive = the reranker moved
    the document up).  Disabled when the ``rerank`` extra is absent.
    """
    stage: dict[str, Any] = {
        "enabled": False,
        "model": None,
        "note": None,
        "ordered": [],
    }
    have_deps = (
        importlib.util.find_spec("sentence_transformers") is not None
        and importlib.util.find_spec("torch") is not None
    )
    if not have_deps:
        stage["note"] = (
            "cross-encoder rerank requires the `rerank` extra "
            "(sentence-transformers + torch); first-stage fusion is used as-is"
        )
        return stage
    try:
        from core.eval.reranker import CrossEncoderReranker

        pool = [row["doc_id"] for row in fused][:depth]
        fusion_rank = {doc_id: rank for rank, doc_id in enumerate(pool, start=1)}
        candidates = [(doc_id, _DOCS[doc_id]["text"]) for doc_id in pool]
        reranked = CrossEncoderReranker(DEFAULT_RERANKER_MODEL).rerank(query, candidates, len(pool))
        stage["enabled"] = True
        stage["model"] = DEFAULT_RERANKER_MODEL
        stage["ordered"] = [
            {
                "doc_id": doc_id,
                "rank": new_rank,
                "delta": fusion_rank.get(doc_id, new_rank) - new_rank,
            }
            for new_rank, doc_id in enumerate(reranked, start=1)
        ]
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, report honestly
        stage["note"] = f"rerank unavailable: {type(exc).__name__}: {exc}"
    return stage


def _context_stage(query: str, ordered_ids: list[str]) -> dict[str, Any]:
    """Budget the gate-approved passages with the real ContextCompiler.

    Passages are handed to the compiler as evidence items; the compiler's own
    tokenizer produces the token counts and its structured budgeter decides what
    fits.  Token unit is the compiler's word/CJK-piece count (not a model tokenizer).
    """
    compiler = ContextCompiler(token_budget=_CONTEXT_BUDGET, strategy="structured")
    evidence = [
        {
            "evidence_id": doc_id,
            "source": _DOCS[doc_id]["title"],
            "summary": _DOCS[doc_id]["text"],
        }
        for doc_id in ordered_ids
    ]
    packet = compiler.compile(
        case_id="retrieval-trace",
        query=query,
        memories_by_tier={},
        current_evidence=evidence,
        required_evidence=[],
    )
    selected: list[dict[str, Any]] = []
    for section in packet.sections:
        for item in section.kept:
            if item.kind != "evidence" or not item.item_id:
                continue
            selected.append({
                "doc_id": item.item_id,
                "title": _DOCS[item.item_id]["title"],
                "tokens": item.estimated_tokens,
                "reason": "truncated to fit budget" if item.truncated else "fits context budget",
                "text": _DOCS[item.item_id]["text"],
            })
    return {
        "budget_tokens": compiler.token_budget,
        "total_tokens": packet.estimated_tokens_after,
        "selected": selected,
    }


def _build_kb_hybrid_scenario(query: str | None = None) -> dict[str, Any]:
    """One KB query through the real HybridKB pipeline, traced per stage.

    ``query`` overrides the fixed default demo KB query.  Optional stages degrade to
    ``enabled: false`` with an honest note rather than raising.  This flow is NOT run
    by the live network-RCA scenario — its triggers are honestly ``live: false``
    (exercised only by ``eval_mem_compare`` LongMemEval and the unit tests).
    """
    text = (query or "").strip() or _DEFAULT_QUERY

    # Symmetric query expansion: documents stemmed, query stemmed + synonym-expanded,
    # exactly as the skill-retrieval eval does so all routes share a vocabulary.
    q_transform, d_transform = make_transform("expand")
    base_tokens = tokenize(text)
    q_tokens = q_transform(base_tokens)
    expansions = _extra_expansions(base_tokens, q_tokens)

    doc_ids = [doc["id"] for doc in _CORPUS]
    doc_tokens = {doc["id"]: d_transform(tokenize(doc["text"])) for doc in _CORPUS}
    tag_tokens = {doc["id"]: d_transform([t.lower() for t in doc["tags"]]) for doc in _CORPUS}

    # ── first-stage routes ──────────────────────────────────────────────────────
    q_token_set = set(q_tokens)
    bm25 = BM25Index(doc_tokens)
    bm25_scored = bm25.rank_with_scores(text, _ROUTE_DEPTH, query_tokens=q_tokens)
    # matched = the ACTUAL overlapping (stemmed/expanded) query terms per doc, i.e.
    # exactly the tokens BM25 scored on — no fabrication.
    bm25_matched = {
        doc_id: sorted(q_token_set & set(doc_tokens[doc_id]))
        for doc_id, _ in bm25_scored
    }
    bm25_route = {
        "id": "bm25",
        "label": _ROUTE_LABELS["bm25"],
        "kind": "lexical",
        "enabled": True,
        "note": None,
        "hits": _bm25_hits(bm25_scored, bm25_matched),
    }

    # dense is offline here (no torch/faiss) -> route enabled:false, no "matched".
    dense_route = _dense_route(doc_ids, [doc["text"] for doc in _CORPUS], text, _ROUTE_DEPTH)

    logical_scored = _structured_scored(tag_tokens, q_token_set)
    # matched = the actual curated tags that overlapped the query terms.
    logical_matched = {
        doc_id: sorted({tag for tag in tag_tokens[doc_id] if tag in q_token_set})
        for doc_id, _ in logical_scored
    }
    logical_route = {
        "id": "logical",
        "label": _ROUTE_LABELS["logical"],
        "kind": "structured",
        "enabled": True,
        "note": None,
        "hits": _route_hits(logical_scored, logical_matched),
    }

    routes = [bm25_route, dense_route, logical_route]

    # ── fusion (RRF over whatever routes actually ran) ───────────────────────────
    rankings: dict[str, list[str]] = {"bm25": [d for d, _ in bm25_scored]}
    if dense_route["enabled"]:
        rankings["dense"] = [hit["doc_id"] for hit in dense_route["hits"]]
    rankings["logical"] = [d for d, _ in logical_scored]
    fused = _fuse_with_scores(rankings, _FUSION_K)

    # ── CRAG confidence gate over the BM25 lexical signal ────────────────────────
    decision = crag_gate(bm25_scored, _FUSION_K, hi=_GATE_HI, lo=_GATE_LO)
    gate = {
        "verdict": decision.action,
        "reason": decision.reason,
        "top_score": float(decision.top_score),
        "kept": list(decision.results),
        "action": _GATE_ACTIONS[decision.action],
    }

    # ── rerank (optional) ────────────────────────────────────────────────────────
    # Dense similarity always has a nearest neighbour, including queries with no
    # evidence-bearing vocabulary in this corpus. Once the gate classifies the
    # query as ungrounded, those neighbours remain route diagnostics only and
    # cannot enter fusion, reranking or model context.
    if decision.action == "incorrect":
        fused = []

    rerank = _rerank_stage(text, fused, _ROUTE_DEPTH)

    # ── context compilation over the gate-approved set (fusion fallback) ─────────
    if decision.results:
        context_input = list(decision.results)
    elif rerank["enabled"] and rerank["ordered"]:
        context_input = [row["doc_id"] for row in rerank["ordered"]]
    else:
        context_input = [row["doc_id"] for row in fused]
    context = _context_stage(text, context_input)

    docs = {
        doc["id"]: {
            "title": doc["title"],
            "text": doc["text"],
            "tags": list(doc["tags"]),
            "source": doc["source"],
        }
        for doc in _CORPUS
    }

    return {
        "id": "kb_hybrid",
        "flow": "kb_hybrid",
        "label": {"zh": "管理口锁定 · KB", "en": "Admin lockout · KB"},
        "query": text,
        "intent": {
            "tier": "library_recall",
            "zh": "简单知识库事实查询：注意力控制器召回相关只读知识文档",
            "en": "A simple KB factual lookup: the attention controller recalls a relevant read-only knowledge passage",
        },
        "corpus": {"size": len(_CORPUS), "source": _CORPUS_SOURCE},
        "triggers": {
            "count": 0,
            "live": False,
            "source": "no production trace; eval-only",
            "note": {
                "zh": "由 eval_mem_compare LongMemEval 与单元测试驱动，并非线上网络 RCA 场景运行",
                "en": "exercised by eval_mem_compare LongMemEval + unit tests, not the live scenario",
            },
        },
        "query_expansions": expansions,
        "routes": routes,
        "fusion": {"method": "rrf", "k": _FUSION_K, "c": 60, "ranked": fused},
        "gate": gate,
        "rerank": rerank,
        "context": context,
        "docs": docs,
    }


# ── network-RCA memory-recall flow ─────────────────────────────────────────────
# These mirror the real call the flagship scenario makes in
# core.orchestrator.orchestrator.SingleAgentRCAOrchestrator._run (node_name
# "memory.retrieve", attributes limit_per_tier=6, graph_depth=2).
_MEM_LIMIT_PER_TIER = 6
_MEM_GRAPH_DEPTH = 2
_MEM_GRAPH_CANDIDATE_LIMIT = 48
_MEM_CONTEXT_BUDGET = 512

# Repo root: core/memory/retrieval_trace.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MEM_SEED_PATH = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "memory_seed.json"
_MEM_SEED_SOURCE = "domains/network_rca/fixtures/memory_seed.json"
_MEM_CASES_PATH = _REPO_ROOT / "domains" / "network_rca" / "seed_cases" / "seed_cases.json"
# The phase-1 trace is the ONLY persisted network-RCA trace; it carries memory_read
# for every one of its five cases, which is the honest live trigger count.
_MEM_TRIGGER_SOURCE = "artifacts/network_rca_phase1_trace.jsonl · memory_read"

# The real network-RCA cases surfaced as memory-recall worked examples.  Each is a
# genuine case from domains/network_rca/seed_cases/seed_cases.json, run through the
# real TieredMemoryStore for THAT case's real query — no self-Q&A, no invented hit.
_MEM_CASES: tuple[tuple[str, dict[str, str]], ...] = (
    ("case_eno1_no_carrier", {"zh": "eno1 无载波", "en": "eno1 no carrier"}),
    ("case_showroom_office_unreachable", {"zh": "showroom→office 不可达", "en": "showroom→office unreachable"}),
)

_MEM_TIER_ORDER: tuple[str, ...] = ("asset_profile", "procedural", "episodic", "semantic")
_MEM_TIER_LABELS: dict[str, dict[str, str]] = {
    "asset_profile": {"zh": "资产画像", "en": "Asset profile"},
    "procedural": {"zh": "程序性记忆 · 运维手册", "en": "Procedural (runbooks)"},
    "episodic": {"zh": "情节记忆 · 历史事件", "en": "Episodic (incident traces)"},
    "semantic": {"zh": "语义记忆 · 反思洞察", "en": "Semantic (reflected insights)"},
}


def _memory_title(text: str, limit: int = 72) -> str:
    """Concise title = the record's own leading clause (real text, not authored)."""
    flat = " ".join(text.split())
    head = flat.split(". ", 1)[0]
    if head != flat and not head.endswith("."):
        head = head + "."
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def _load_memory_case(case_id: str) -> tuple[dict[str, Any], list[MemoryRecord]]:
    """Load a real network-RCA case + its seeded tiered-memory records."""
    raw_cases = json.loads(_MEM_CASES_PATH.read_text(encoding="utf-8"))
    case = next(item["case"] for item in raw_cases if item["case"]["id"] == case_id)
    raw_records = json.loads(_MEM_SEED_PATH.read_text(encoding="utf-8"))
    records = [MemoryRecord.model_validate(item) for item in raw_records]
    return case, records


def _memory_matched(
    query_terms: list[str], assets: list[str], record: MemoryRecord,
) -> list[str]:
    """REAL reason a memory surfaced: overlapping (lowercased) query terms against
    the record's own lexical tokens (text + tags), plus exact-identity asset hits.

    Every element is a genuine intersection with the seeded record — nothing is
    authored.  Lexical overlap is what BM25 scored on; asset overlap is the
    exact-identity boost the store applies on top.
    """
    q_terms = {term.lower() for term in query_terms if term}
    doc_tokens = set(tokenize(record.text)) | {tag.lower() for tag in record.tags}
    lexical = q_terms & doc_tokens
    asset_hits = set(assets) & set(record.asset_ids)
    return sorted(lexical | asset_hits)


def _memory_via(diag: dict[str, Any]) -> list[str]:
    """Which real signal(s) surfaced this record: lexical / vector / graph.

    Only signals that actually fired in THIS environment are listed.  No dense
    index is attached to the trace's store, so ``vector`` never appears here; a
    record reached by a structural hop (``graph_hop`` > 0) is tagged ``graph``.
    """
    via: list[str] = []
    if float(diag.get("lexical_score", 0.0)) > 0.0:
        via.append("lexical")
    if float(diag.get("vector_score", 0.0)) > 0.0:
        via.append("vector")
    if int(diag.get("graph_hop", 0)) > 0:
        via.append("graph")
    return via


def _memory_context_stage(query: str, memories_by_tier: dict[str, list[MemoryRecord]]) -> dict[str, Any]:
    """Budget the recalled memories with the real ContextCompiler (structured)."""
    compiler = ContextCompiler(token_budget=_MEM_CONTEXT_BUDGET, strategy="structured")
    packet = compiler.compile(
        case_id="retrieval-trace-memory",
        query=query,
        memories_by_tier=memories_by_tier,
        current_evidence=[],
        required_evidence=[],
    )
    by_id = {
        record.memory_id: record
        for records in memories_by_tier.values()
        for record in records
    }
    selected: list[dict[str, Any]] = []
    for section in packet.sections:
        for item in section.kept:
            if item.kind not in _MEM_TIER_ORDER or not item.item_id:
                continue
            record = by_id.get(item.item_id)
            selected.append({
                "doc_id": item.item_id,
                "title": _memory_title(record.text) if record else item.item_id,
                "tokens": item.estimated_tokens,
                "reason": "truncated to fit budget" if item.truncated else "fits context budget",
                "text": record.text if record else "",
            })
    return {
        "budget_tokens": compiler.token_budget,
        "total_tokens": packet.estimated_tokens_after,
        "selected": selected,
    }


def _graph_relation(record: MemoryRecord | None, target_id: str) -> str | None:
    """The real edge label of a walked graph hop, if the store exposes one.

    The bounded walk in :meth:`TieredMemoryStore.retrieve` follows ``record.links``,
    which carry no type; a typed label only exists when the source record also
    declares a :class:`MemoryRelation` to the same target.  Returned only then —
    otherwise the caller omits ``relation`` rather than inventing one.
    """
    if record is None:
        return None
    for relation in record.relations:
        if relation.target_id == target_id:
            return relation.relation_type
    return None


def _build_memory_recall_case(case_id: str, label: dict[str, str]) -> dict[str, Any]:
    """Reconstruct ONE real network-RCA memory-recall worked example from fixtures.

    Seeds a :class:`TieredMemoryStore` from the domain's ``memory_seed.json`` and
    runs the exact ``retrieve`` call the orchestrator makes (``limit_per_tier=6``,
    ``graph_depth=2``) for THIS case's real query.  Every score/rank/tier/hit is
    genuine store output; every hit carries the record's real body ``text`` and the
    REAL ``matched`` terms/assets that surfaced it.  This flow deliberately carries
    no routes/fusion/gate/rerank — it does not use them.
    """
    case, records = _load_memory_case(case_id)
    query = str(case["query"])
    query_terms = list(case.get("query_terms", []))
    assets = list(case.get("assets", []))

    store = TieredMemoryStore(enabled=True)
    store.seed(records)
    memories_by_tier = store.retrieve(
        query_terms,
        assets,
        limit_per_tier=_MEM_LIMIT_PER_TIER,
        graph_depth=_MEM_GRAPH_DEPTH,
        graph_candidate_limit=_MEM_GRAPH_CANDIDATE_LIMIT,
    )
    diagnostics = {item["memory_id"]: item for item in store.retrieval_diagnostics()}
    by_id = {record.memory_id: record for record in records}

    tiers: list[dict[str, Any]] = []
    for tier in _MEM_TIER_ORDER:
        hits: list[dict[str, Any]] = []
        for rank, record in enumerate(memories_by_tier.get(tier, []), start=1):
            diag = diagnostics.get(record.memory_id, {})
            hits.append({
                "doc_id": record.memory_id,
                "rank": rank,
                "score": float(diag.get("final_score", 0.0)),
                "title": _memory_title(record.text),
                "snippet": _snippet(record.text),
                "text": record.text,
                "via": _memory_via(diag),
                "matched": _memory_matched(query_terms, assets, record),
            })
        tiers.append({
            "id": tier,
            "label": _MEM_TIER_LABELS[tier],
            "kind": tier,
            "hits": hits,
        })

    # Structural graph-hop expansion (graph_depth=2): the seed memories carry no
    # A-MEM links, so the bounded walk expands nothing — reported honestly.
    expanded: list[dict[str, Any]] = []
    for mem_id, diag in diagnostics.items():
        if int(diag.get("graph_hop", 0)) <= 0:
            continue
        parent_id = diag.get("graph_parent_id")
        row: dict[str, Any] = {"doc_id": mem_id, "from": parent_id, "hop": int(diag["graph_hop"])}
        relation = _graph_relation(by_id.get(parent_id) if parent_id else None, mem_id)
        if relation is not None:
            row["relation"] = relation
        expanded.append(row)
    graph: dict[str, Any] | None = {"hops": _MEM_GRAPH_DEPTH, "expanded": expanded}

    context = _memory_context_stage(query, memories_by_tier)

    docs = {
        record.memory_id: {
            "title": _memory_title(record.text),
            "text": record.text,
            "tags": list(record.tags),
            "source": f"{_MEM_SEED_SOURCE} · {record.tier}",
        }
        for record in records
    }

    return {
        "id": case_id,
        "flow": "memory_recall",
        "label": label,
        "query": query,
        "intent": {
            "tier": "deep_agent",
            "zh": "诊断类网络 RCA 事件升级至深度智能体（规划-执行-批判）拓扑，首先做分层记忆召回",
            "en": "Diagnostic network-RCA incidents escalate to the deep-agent (planner-executor-critic) topology, which first runs tiered memory recall",
        },
        "corpus": {"size": len(records), "source": f"{_MEM_SEED_SOURCE} (seeded tiered memory)"},
        "triggers": {
            "count": 5,
            "live": True,
            "source": _MEM_TRIGGER_SOURCE,
            "note": {
                "zh": "唯一落盘的 phase-1 轨迹中的 5 个网络 RCA 案例（memory_read × 5）",
                "en": "5 network-RCA cases in the one persisted phase-1 trace (memory_read × 5)",
            },
        },
        "tiers": tiers,
        "graph": graph,
        "context": context,
        "docs": docs,
    }


def build_retrieval_trace(
    lang: str = "zh", query: str | None = None, scenario: str = "live",
) -> dict[str, Any]:
    """Return the worked-example retrieval trace for ``/api/rca/retrieval``.

    Returns ``{"ok": true, "cases": [...]}`` with REAL worked examples so the
    frontend can render a readable case record.  ``scenario`` selects which real flow
    the page shows — the two flows are DIFFERENT real retrieval paths in this
    codebase, so the switch swaps the page's data source between them without
    fabricating anything:

    * ``scenario="live"`` (default) → ONLY the ``memory_recall`` cases: the flagship
      REAL internal-network RCA flow, each a different real seed case run through the
      real ``TieredMemoryStore`` for its own real query.
    * ``scenario="bench"`` → ONLY the ``kb_hybrid`` case: the corpus/KB benchmark flow
      (BM25 + optional dense + structured routes, RRF fusion, CRAG gate, optional
      rerank), exercised by the eval harnesses and honestly ``live: false``.

    Every hit carries the real document ``text`` and the REAL ``matched`` terms/assets
    that surfaced it — the same real cases, just filtered by scenario.

    ``lang`` is accepted for parity with the other builders; human-readable strings
    are returned as ``{"zh": ..., "en": ...}`` objects so the frontend selects the
    language and no text is pre-flattened server-side.  ``query`` overrides the fixed
    default KB query of the ``kb_hybrid`` case only (i.e. only in ``bench``); the
    memory-recall cases are pinned to their real network-RCA case queries.  Always
    returns ``ok: true`` — optional stages degrade to ``enabled: false`` with an
    honest note.
    """
    scenario = str(scenario or "live").lower()
    if scenario == "bench":
        cases = [_build_kb_hybrid_scenario(query)]
    else:  # "live" (default): only the real internal-network memory-recall flow
        cases = [_build_memory_recall_case(case_id, label) for case_id, label in _MEM_CASES]
    return {"ok": True, "cases": cases}


_DOCS: dict[str, dict[str, Any]] = {doc["id"]: doc for doc in _CORPUS}


__all__ = ["build_retrieval_trace"]
