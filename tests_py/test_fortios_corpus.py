"""Tests for the OPTIONAL FortiOS ops-KB RAG eval (core.eval.fortios_corpus).

The corpus builder, ToC/HTML parsers, structure-aware chunker, deterministic
Contextual-Retrieval header, and the non-circular label file are PURE stdlib, so they
are tested hermetically here (no network, no model, no cached corpus) and run in the
default system-python suite. The one end-to-end test that needs the embedding/rerank
stack + the built corpus is gated: it importorskips sentence-transformers and skips if
the corpus cache is absent, so `python3 -m pytest tests_py/ -q` stays green everywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.eval import fortios_corpus as FC


# ── ToC hierarchy parser (nested <ul class="toc">, rendered twice per page) ──────────
_TOC_HTML = """
<html><body>
<!-- desktop, fully nested -->
<ul class="toc"><li class="toc toc-item">
  <a class="toc" href="/document/fortigate/7.4.0/administration-guide/100/parent">Parent</a>
  <ul class="toc"><li class="toc toc-item">
    <a class="toc" href="/document/fortigate/7.4.0/administration-guide/200/child-sec">Child Sec</a>
    <ul class="toc"><li class="toc toc-item">
      <a class="toc" href="/document/fortigate/7.4.0/administration-guide/300/leaf">Leaf</a>
    </li></ul>
  </li></ul>
</li></ul>
<!-- mobile, FLAT: must not overwrite the nested ancestors above -->
<ul class="toc"><li class="toc toc-item">
  <a class="toc" href="/document/fortigate/7.4.0/administration-guide/300/leaf">Leaf</a>
</li></ul>
</body></html>
"""


def test_parse_toc_builds_hierarchy_and_prefers_deepest():
    toc = FC.parse_toc(_TOC_HTML)
    assert set(toc) == {"100", "200", "300"}
    assert toc["100"] == ("Parent", "parent", [])
    assert toc["200"] == ("Child Sec", "child-sec", ["Parent"])
    # the flat second rendering must NOT flatten the leaf's ancestor chain
    assert toc["300"] == ("Leaf", "leaf", ["Parent", "Child Sec"])


# ── main-content extraction (only mc-main-content; footer/scripts excluded) ──────────
_PAGE_HTML = """
<html><body>
<div class="breadcrumbs">Home > FortiGate</div>
<div id="content">
  <div role="main" id="mc-main-content">
    <h1>Configuring an LDAP server</h1>
    <p>FortiOS can be configured to use an LDAP server for authentication.</p>
    <h2>Details</h2>
    <p>When configuring an LDAP connection.</p>
    <ul><li>Go to User &amp; Authentication.</li></ul>
    <script>var x = "ignore me";</script>
    <table><tr><td>cell text</td></tr></table>
  </div>
</div>
<div id="footer">footer text must be excluded</div>
</body></html>
"""


def test_extract_blocks_captures_main_only():
    blocks = FC.extract_blocks(_PAGE_HTML)
    tags = [t for t, _ in blocks]
    texts = [x for _, x in blocks]
    assert blocks[0] == ("h1", "Configuring an LDAP server")
    assert ("h2", "Details") in blocks
    assert "Go to User & Authentication." in texts        # entity decoded, li captured
    assert "cell text" in texts                            # table cell captured
    assert all("footer" not in x for x in texts)           # footer excluded
    assert all("ignore me" not in x for x in texts)        # script excluded


def test_extract_blocks_empty_when_no_main():
    assert FC.extract_blocks("<html><body><p>no main here</p></body></html>") == []


# ── deterministic Contextual-Retrieval header ───────────────────────────────────────
def test_context_header_is_hierarchy_breadcrumb():
    h = FC._context_header(["FortiOS 7.4 Administration Guide", "User & Authentication", "LDAP servers"],
                           ["To configure an LDAP server"])
    assert h == ("FortiOS 7.4 Administration Guide > User & Authentication > LDAP servers "
                 "> To configure an LDAP server")


def test_context_header_dedupes_repeated_tail():
    # if the in-page heading equals the section title, it is not duplicated.
    h = FC._context_header(["Doc", "Chapter", "Section"], ["Section"])
    assert h == "Doc > Chapter > Section"


# ── structure-aware chunking ─────────────────────────────────────────────────────────
def test_chunk_section_structure_and_cr_header():
    blocks = [
        ("h1", "Firewall policy"),
        ("p", "Intro paragraph about policies. " * 3),
        ("h2", "Parameters"),
        ("p", "Parameter details. " * 3),
    ]
    chunks = FC.chunk_section("656084", "Firewall policy", ["Policy and Objects", "Policies"], blocks)
    assert chunks, "should produce at least one chunk"
    for c in chunks:
        # h1/section title never leaks into the body (it lives in the CR header)
        assert "Firewall policy" not in c.text
        # CR header is the deterministic hierarchy breadcrumb
        assert c.context_header.startswith(FC.DOC_TITLE + " > Policy and Objects > Policies > Firewall policy")
        # cr_text = header + body
        assert c.cr_text.startswith(c.context_header)
        assert c.section_id == "656084"
    # chunk ids are unique and ordered
    ids = [c.id for c in chunks]
    assert ids == sorted(set(ids), key=ids.index)


def test_chunk_section_deterministic():
    blocks = [("h1", "T"), ("h2", "Sub"), ("p", "Body text here that is reasonably long. " * 4)]
    a = FC.chunk_section("1", "T", ["Anc"], blocks)
    b = FC.chunk_section("1", "T", ["Anc"], blocks)
    assert [c.__dict__ for c in a] == [c.__dict__ for c in b]


def test_chunk_section_splits_on_major_heading():
    # two h2 blocks each with enough body -> at least two chunks (never straddle h1/h2).
    body = "Sentence with several words to add length. " * 8
    blocks = [("h1", "Sec"), ("h2", "A"), ("p", body), ("h2", "B"), ("p", body)]
    chunks = FC.chunk_section("9", "Sec", [], blocks)
    assert len(chunks) >= 2
    trails = {tuple(c.heading_trail) for c in chunks}
    assert ("A",) in trails and ("B",) in trails


# ── non-circular labels (committed fortios_labels.json) ──────────────────────────────
def test_labels_file_is_wellformed_and_noncircular():
    spec = json.loads((Path(FC.__file__).parent / "fortios_labels.json").read_text())
    labels = spec["labels"]
    assert len(labels) == 6
    ids = {l["incident_id"] for l in labels}
    assert "real_admin_bruteforce_lockout" in ids and "real_dhcp_service_health" in ids
    for l in labels:
        assert l["relevant_sections"], f"{l['incident_id']} has no relevant sections"
        # every relevant section is a numeric docs.fortinet.com id with a written rationale
        for sid in l["relevant_sections"]:
            assert sid.isdigit()
            assert sid in l["rationale"] and len(l["rationale"][sid]) > 40
        assert l["query_fallback"]
    # the non-circularity argument is documented in code and in the file
    assert "semantic" in FC.why_labels_are_noncircular().lower()
    assert spec["why_noncircular"]


def test_load_labels_uses_authored_fallback_without_heldout(monkeypatch):
    # when the gitignored held-out fixture is absent, the authored query is used.
    monkeypatch.setattr(FC, "_HELDOUT_PATH", Path("/nonexistent/heldout_cases.json"))
    items = FC.load_labels()
    assert len(items) == 6
    assert all(it["query_source"] == "authored_fallback" for it in items)
    assert all(it["query"] and it["relevant_sections"] for it in items)


# ── pure eval helpers ────────────────────────────────────────────────────────────────
def test_collapse_to_sections_first_appearance_order():
    chunk_to_section = {"a#0": "a", "a#1": "a", "b#0": "b", "c#0": "c"}
    ranking = ["a#1", "a#0", "b#0", "c#0"]
    assert FC._collapse_to_sections(ranking, chunk_to_section) == ["a", "b", "c"]


def test_stage_delta_signs_and_relative():
    base = {5: {"recall_at_k": 0.40, "ndcg_at_k": 0.50}}
    new = {5: {"recall_at_k": 0.50, "ndcg_at_k": 0.45}}
    d = FC._stage_delta(base, new, (5,))
    assert d["recall_at_k@5"]["abs_delta"] == pytest.approx(0.10)
    assert d["recall_at_k@5"]["rel_delta_pct"] == pytest.approx(25.0)
    assert d["ndcg_at_k@5"]["abs_delta"] == pytest.approx(-0.05)  # honest: a stage can hurt


# ── gated end-to-end (needs the extras AND a built corpus cache) ─────────────────────
def _corpus_cached() -> bool:
    return FC._CORPUS_JSON.exists()


def _dense_cache_ready() -> bool:
    # the CR-chunk embedding cache (.npy) — its absence would make the eval re-embed the
    # whole corpus (minutes on CPU), so skip the end-to-end test rather than pay that here.
    return bool(list(FC._CACHE_DIR.glob("*fortios_cr_*.npy")))


@pytest.mark.skipif(not _corpus_cached(), reason="FortiOS corpus not built (run: python -m core.eval.fortios_corpus build)")
def test_corpus_shape_when_built():
    corpus = FC.load_corpus()
    assert corpus["n_chunks"] > 500                       # a real multi-hundred-chunk corpus
    assert corpus["n_sections"] > 100
    # all labelled relevant sections must exist in the built corpus (else the eval is broken)
    present = {c["section_id"] for c in corpus["chunks"]}
    for lab in FC.load_labels(use_heldout=False):
        for sid in lab["relevant_sections"]:
            assert sid in present, f"labelled section {sid} missing from corpus"


@pytest.mark.skipif(not (_corpus_cached() and _dense_cache_ready()),
                    reason="FortiOS corpus or CR embedding cache not built")
def test_fortios_rag_eval_end_to_end():
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")
    res = FC.run_fortios_rag_eval(k_values=(1, 5, 10), include_rerank=False)
    assert res["n_queries"] == 6
    assert set(res["stages"]) == {"bm25_raw", "bm25_cr", "hybrid_cr"}
    for s in res["stages"]:
        for k in (1, 5, 10):
            m = res["results"][s][k]
            assert 0.0 <= m["recall_at_k"] <= 1.0
            assert 0.0 <= m["ndcg_at_k"] <= 1.0
