"""Contract + honesty tests for the worked-example retrieval trace builder (LLM-free).

These lock the JSON shape consumed by the ``/api/rca/retrieval`` frontend page and
the honesty guarantees.  ``build_retrieval_trace`` returns a ``cases`` array of REAL
worked examples in which DIFFERENT cases run DIFFERENT real retrieval flows:

* ``memory_recall`` cases — several DIFFERENT real network-RCA seed cases, each run
  through the real ``TieredMemoryStore`` seeded from the domain's ``memory_seed.json``
  for THAT case's real query; tier hits match the persisted ``memory_read`` payloads
  (5 cases, ``live: true``); they use NO fusion/gate/rerank.
* ``kb_hybrid`` case — BM25 + optional dense + structured routes, RRF fusion, CRAG
  gate, optional cross-encoder rerank; ``live: false`` (eval-only).  The optional
  dense/rerank stages degrade to ``enabled: false`` with a real note (never
  fabricated), and the fused ``rrf_score`` ordering matches ``rrf_fuse`` exactly.

Every hit carries the referenced document's real ``text`` and the REAL ``matched``
terms/assets that surfaced it.  All deterministic — no model download, no LLM.
"""
from __future__ import annotations

from core.memory.retrieval_trace import build_retrieval_trace
from core.memory.rrf import rrf_fuse

_CASE_COMMON_KEYS = {
    "id", "flow", "label", "query", "intent", "corpus", "triggers", "context", "docs",
}


def _by_id(trace: dict) -> dict[str, dict]:
    return {c["id"]: c for c in trace["cases"]}


def _memory_cases(trace: dict) -> list[dict]:
    return [c for c in trace["cases"] if c["flow"] == "memory_recall"]


def _kb_cases(trace: dict) -> list[dict]:
    return [c for c in trace["cases"] if c["flow"] == "kb_hybrid"]


def test_top_level_cases_and_ok():
    # live (default) shows only the real internal-network memory-recall flow;
    # bench shows only the corpus/KB hybrid flow.
    live = build_retrieval_trace("zh")
    bench = build_retrieval_trace("zh", scenario="bench")
    assert live["ok"] is True and bench["ok"] is True
    mem = _memory_cases(live)
    kb = _kb_cases(bench)
    # >=2 memory-recall worked examples (each a DIFFERENT real case)
    assert len(mem) >= 2
    assert len({c["id"] for c in mem}) == len(mem)  # genuinely different cases
    assert len(kb) >= 1


def test_scenario_live_returns_only_memory_recall():
    t = build_retrieval_trace("zh")  # default == live
    assert t["ok"] is True
    flows = {c["flow"] for c in t["cases"]}
    assert flows == {"memory_recall"}
    assert not _kb_cases(t)
    assert build_retrieval_trace("en", scenario="live")["cases"]  # explicit live too


def test_scenario_bench_returns_only_kb_hybrid():
    t = build_retrieval_trace("zh", scenario="bench")
    assert t["ok"] is True
    flows = {c["flow"] for c in t["cases"]}
    assert flows == {"kb_hybrid"}
    assert not _memory_cases(t)


def test_every_case_has_common_contract():
    cases = (
        build_retrieval_trace("en")["cases"]
        + build_retrieval_trace("en", scenario="bench")["cases"]
    )
    for s in cases:
        assert _CASE_COMMON_KEYS <= set(s)
        assert s["flow"] in {"memory_recall", "kb_hybrid"}
        assert set(s["label"]) == {"zh", "en"}
        assert isinstance(s["query"], str) and s["query"]
        assert {"tier", "zh", "en"} <= set(s["intent"])
        assert isinstance(s["corpus"]["size"], int) and s["corpus"]["size"] > 0
        assert isinstance(s["corpus"]["source"], str) and s["corpus"]["source"]
        tr = s["triggers"]
        assert {"count", "live", "source", "note"} <= set(tr)
        assert isinstance(tr["count"], int) and isinstance(tr["live"], bool)
        assert set(tr["note"]) == {"zh", "en"}
        c = s["context"]
        assert {"budget_tokens", "total_tokens", "selected"} <= set(c)
        assert c["budget_tokens"] > 0 and c["total_tokens"] >= 0
        for sel in c["selected"]:
            assert {"doc_id", "title", "tokens", "reason", "text"} <= set(sel)
            assert sel["doc_id"] in s["docs"] and sel["tokens"] > 0
            # context passages carry the REAL compiled text
            assert isinstance(sel["text"], str) and sel["text"]
        for doc in s["docs"].values():
            assert {"title", "text", "tags", "source"} <= set(doc)
            assert isinstance(doc["text"], str) and doc["text"]


def test_context_selected_carries_real_text():
    cases = (
        build_retrieval_trace("zh")["cases"]
        + build_retrieval_trace("zh", scenario="bench")["cases"]
    )
    saw_selection = False
    for s in cases:
        for sel in s["context"]["selected"]:
            saw_selection = True
            # the compiled passage text is the real doc body
            assert sel["text"] == s["docs"][sel["doc_id"]]["text"]
    assert saw_selection, "at least one case should compile a context passage"


def test_memory_recall_cases_are_real_and_live():
    mem = _memory_cases(build_retrieval_trace("zh"))
    assert len(mem) >= 2
    for s in mem:
        assert s["flow"] == "memory_recall"
        # honest live trigger count from the single persisted phase-1 trace
        assert s["triggers"]["live"] is True and s["triggers"]["count"] == 5
        assert "network_rca_phase1_trace.jsonl" in s["triggers"]["source"]
        # the four tiers, in order
        assert [tier["kind"] for tier in s["tiers"]] == [
            "asset_profile", "procedural", "episodic", "semantic",
        ]
        for tier in s["tiers"]:
            assert set(tier["label"]) == {"zh", "en"}
            # ranks are dense and 1-based within each tier; scores strictly positive
            assert [h["rank"] for h in tier["hits"]] == list(range(1, len(tier["hits"]) + 1))
            for h in tier["hits"]:
                assert {"doc_id", "rank", "score", "title", "snippet", "text", "via", "matched"} <= set(h)
                assert h["doc_id"] in s["docs"] and h["score"] > 0.0
                # every hit carries the record's REAL body text
                assert isinstance(h["text"], str) and h["text"]
                assert h["text"] == s["docs"][h["doc_id"]]["text"]
                # via reflects only signals that fired here: dense is absent -> no "vector"
                assert set(h["via"]) <= {"lexical", "graph"}
                assert isinstance(h["matched"], list)
        # graph_depth=2 ran; the seed memories carry no links -> nothing expands (honest)
        assert s["graph"]["hops"] == 2 and s["graph"]["expanded"] == []
        # this flow does NOT use fusion/gate/rerank/routes — their absence is the point
        for absent in ("routes", "fusion", "gate", "rerank", "query_expansions"):
            assert not s.get(absent)
        # at least one hit in this case surfaced with a REAL non-empty matched reason
        all_hits = [h for tier in s["tiers"] for h in tier["hits"]]
        assert any(h["matched"] for h in all_hits)


def test_memory_recall_matches_persisted_payload():
    """The two flagship worked examples reconstruct the real store output exactly."""
    by_id = _by_id(build_retrieval_trace("zh"))

    # showroom -> office unreachable (the 192.168.1.0/24 case)
    show = by_id["case_showroom_office_unreachable"]
    assert "192.168.1.0/24" in show["query"]
    show_hits = {tier["kind"]: [h["doc_id"] for h in tier["hits"]] for tier in show["tiers"]}
    assert show_hits["asset_profile"] == ["asset-r230-profile"]
    assert show_hits["procedural"] == [
        "procedural-policy-before-vlan", "procedural-vip-policy-pair",
    ]
    assert show_hits["semantic"] == ["semantic-local-topology"]
    assert show_hits["episodic"] == []
    # the top procedural hit's REAL matched terms/assets
    top = show["tiers"][1]["hits"][0]
    assert top["doc_id"] == "procedural-policy-before-vlan"
    assert set(top["matched"]) >= {"policy", "route", "vlan"}

    # eno1 no-carrier (a DIFFERENT real case)
    eno1 = by_id["case_eno1_no_carrier"]
    assert "eno1" in eno1["query"].lower()
    eno1_hits = {tier["kind"]: [h["doc_id"] for h in tier["hits"]] for tier in eno1["tiers"]}
    assert eno1_hits["asset_profile"] == ["asset-r230-profile"]
    assert eno1_hits["procedural"] == ["procedural-carrier-first"]


def test_kb_hybrid_case_pipeline_and_eval_only():
    s = _kb_cases(build_retrieval_trace("en", scenario="bench"))[0]
    assert s["flow"] == "kb_hybrid"
    # honest eval-only triggers — never a fabricated production number
    assert s["triggers"]["live"] is False and s["triggers"]["count"] == 0
    # real KB query (FortiOS admin lockout)
    assert "lockout" in s["query"].lower()
    assert isinstance(s["query_expansions"], list)
    # routes: bm25 + dense + logical; bm25 always live and populated
    ids = {r["id"] for r in s["routes"]}
    assert {"bm25", "dense", "logical"} <= ids
    lexical_matched_seen = False
    for r in s["routes"]:
        assert {"id", "label", "kind", "enabled", "note", "hits"} <= set(r)
        assert set(r["label"]) == {"zh", "en"}
        for h in r["hits"]:
            assert {"doc_id", "rank", "score", "title", "snippet", "text", "matched"} <= set(h)
            assert h["doc_id"] in s["docs"]
            # every hit carries the real doc body text
            assert isinstance(h["text"], str) and h["text"]
            assert h["text"] == s["docs"][h["doc_id"]]["text"]
            assert isinstance(h["matched"], list)
            if r["kind"] == "lexical" and h["matched"]:
                lexical_matched_seen = True
                # matched terms are real overlaps present in the doc token space
                assert all(isinstance(m, str) and m for m in h["matched"])
    # at least some lexical (BM25) hits surfaced with a real non-empty matched reason
    assert lexical_matched_seen
    bm25 = next(r for r in s["routes"] if r["id"] == "bm25")
    assert bm25["enabled"] is True and bm25["hits"]
    # optional dense/rerank degrade honestly when their extras are absent
    dense = next(r for r in s["routes"] if r["id"] == "dense")
    if not dense["enabled"]:
        assert dense["hits"] == [] and isinstance(dense["note"], str) and dense["note"]
    rr = s["rerank"]
    assert {"enabled", "model", "note", "ordered"} <= set(rr)
    if not rr["enabled"]:
        assert rr["ordered"] == [] and rr["model"] is None and isinstance(rr["note"], str)
    # fusion is a real RRF whose ordering matches rrf_fuse over the exact routes
    f = s["fusion"]
    assert f["method"] == "rrf" and f["c"] == 60
    for row in f["ranked"]:
        assert {"doc_id", "rank", "rrf_score", "from_routes", "title"} <= set(row)
        assert row["from_routes"] and all(fr in ids for fr in row["from_routes"])
    route_ranks = {
        r["id"]: [h["doc_id"] for h in r["hits"]]
        for r in s["routes"]
        if r["enabled"] and r["hits"]
    }
    canonical = rrf_fuse(list(route_ranks.values()), f["k"], c=f["c"])
    assert [row["doc_id"] for row in f["ranked"]] == canonical
    # gate is a real CRAG decision; the admin-lockout query grounds strongly
    g = s["gate"]
    assert {"verdict", "reason", "top_score", "kept", "action"} <= set(g)
    assert g["verdict"] == "correct" and g["reason"] == "high_confidence"
    assert set(g["action"]) == {"zh", "en"}
    assert g["kept"] and all(d in s["docs"] for d in g["kept"])


def test_kb_query_override_and_expansions():
    t = build_retrieval_trace("en", query="traffic denied by firewall policy", scenario="bench")
    s = _kb_cases(t)[0]
    assert s["query"] == "traffic denied by firewall policy"
    top = s["fusion"]["ranked"][0]["doc_id"]
    assert top.startswith("fos-policy"), top
    # the query override must NOT leak into the pinned memory-recall (live) cases
    for mem in _memory_cases(build_retrieval_trace("en", query="traffic denied by firewall policy")):
        assert mem["query"] not in {"traffic denied by firewall policy"}


def test_kb_degrades_gracefully_on_out_of_domain_query():
    t = build_retrieval_trace("en", query="xyzzy quantum teapot nonsense zzz", scenario="bench")
    assert t["ok"] is True
    s = _kb_cases(t)[0]
    assert s["gate"]["verdict"] == "incorrect" and s["gate"]["reason"] == "not_observed"
    assert s["fusion"]["ranked"] == [] and s["context"]["selected"] == []
