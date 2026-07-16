"""Tests for the REAL IODA-v2 three-source evidence-retrieval eval.

Two layers:
  * synthetic unit tests (no data file) pin the core structured-match logic —
    typed entity matching, the time gate, and the "entity-or-time required" rule;
  * real-data tests (skipped if the pool is absent) lock the honest headline
    numbers, prove the BM25 candidate optimisation is exact, and — most importantly
    — self-check that no query leaks an answer id and no document text carries a
    label field. Everything is LLM-free and deterministic.
"""
from __future__ import annotations

import pytest

from core.eval import ioda_retrieval as R

# ── synthetic fixtures (data-file independent) ──────────────────────────────────
_IDENTITY = (lambda toks: toks)


def _rec(evidence_id, event_id, entity_id, entity_type, signal_type, source, time_bin, **topo):
    return {
        "evidence_id": evidence_id, "candidate_event_id": event_id,
        "entity_id": entity_id, "entity_type": entity_type,
        "source": source, "signal_type": signal_type, "phase": "onset",
        "time_bin": time_bin,
        "topology": {"as_path": None, "collector": None, "origin_as": None,
                     "peer_asn": None, "prefix": None, **topo},
    }


def _event(event_id, *, locations=(), asns=(), start="2022-03-01T00:00:00+00:00",
           end="2022-03-02T00:00:00+00:00", outage_type="NATIONWIDE",
           outage_cause="POWER_OUTAGE", datasources=()):
    return {"event_id": event_id, "locations": list(locations), "asns": list(asns),
            "event_start": start, "event_end": end, "outage_type": outage_type,
            "outage_cause": outage_cause, "ioda_v2_datasources": list(datasources)}


def test_structured_entity_match_is_typed_not_just_digits():
    # a country doc whose id is "800" must NOT match an ASN query token "800".
    rec = _rec("radar:800::x", "radar:800", "800", "country", "radar_outage",
               "cloudflare_radar", "2022-06-06T00:00:00+00:00")   # out of the event window below
    corpus = R.Corpus((rec,), _IDENTITY)
    did = R._doc_id("radar:800::x")
    q = R.Query(_event("q", asns=["800"]), _IDENTITY)             # ASN 800, different window
    assert R._structured_score(corpus, q, did, use_time=True) == 0.0
    # same digits but typed as ASN -> matches on the entity axis.
    rec_asn = _rec("radar:x::y", "radar:x", "800", "asn", "bgp_update_delta",
                   "bgp_routeviews_ris", "2022-06-06T00:00:00+00:00")
    corpus2 = R.Corpus((rec_asn,), _IDENTITY)
    q2 = R.Query(_event("q", asns=["800"]), _IDENTITY)
    assert R._structured_score(corpus2, q2, R._doc_id("radar:x::y"), use_time=True) >= R._W_ENTITY


def test_structured_time_gate_and_entity_or_time_required():
    inwin = _rec("e:in", "e", "US", "country", "radar_outage", "cloudflare_radar",
                 "2022-03-01T12:00:00+00:00")
    outwin = _rec("e:out", "e", "US", "country", "radar_outage", "cloudflare_radar",
                  "2022-09-01T12:00:00+00:00")
    other = _rec("e:oth", "e", "ZZ", "country", "radar_outage", "cloudflare_radar",
                 "2022-09-01T12:00:00+00:00")  # wrong entity AND out of window
    corpus = R.Corpus((inwin, outwin, other), _IDENTITY)
    q = R.Query(_event("e", locations=["US"]), _IDENTITY)
    s_in = R._structured_score(corpus, q, R._doc_id("e:in"), use_time=True)
    s_out = R._structured_score(corpus, q, R._doc_id("e:out"), use_time=True)
    s_oth = R._structured_score(corpus, q, R._doc_id("e:oth"), use_time=True)
    assert s_in > s_out > 0.0            # entity+time beats entity-only
    assert s_oth == 0.0                  # neither axis -> not a candidate
    # with the time axis removed, in-window and out-window score the same.
    assert R._structured_score(corpus, q, R._doc_id("e:in"), use_time=False) == \
           R._structured_score(corpus, q, R._doc_id("e:out"), use_time=False)


def test_structured_retriever_ranks_in_window_first_and_drops_noncandidates():
    inwin = _rec("e:in", "e", "US", "country", "radar_outage", "cloudflare_radar",
                 "2022-03-01T12:00:00+00:00")
    outwin = _rec("e:out", "e", "US", "country", "radar_outage", "cloudflare_radar",
                  "2022-09-01T12:00:00+00:00")
    unrelated = _rec("z:1", "z", "ZZ", "country", "radar_outage", "cloudflare_radar",
                     "2022-09-01T12:00:00+00:00")
    corpus = R.Corpus((inwin, outwin, unrelated), _IDENTITY)
    retrieve = R._structured_retriever(corpus, use_time=True)
    got = retrieve(R.Query(_event("e", locations=["US"]), _IDENTITY), 10)
    assert got == [R._doc_id("e:in"), R._doc_id("e:out")]   # unrelated never candidate


def test_doc_id_is_stable_content_hash():
    assert R._doc_id("radar:225::radar_outage::onset") == R._doc_id("radar:225::radar_outage::onset")
    assert R._doc_id("a") != R._doc_id("b")
    assert R._doc_id("radar:225::x").startswith("d")


# ── real-data tests ─────────────────────────────────────────────────────────────
_DATA_OK = (R._data_dir(None) / R._EVIDENCE_NAME).exists()
real_data = pytest.mark.skipif(not _DATA_OK, reason="real IODA v2 pool not present")


@real_data
def test_corpus_doc_ids_unique_no_hash_collision():
    corpus = R.build_corpus("base")
    assert len(corpus.doc_tokens) == 8542            # every record kept, ids unique
    assert len(R.load_evidence()) == 8542


@real_data
def test_bm25_candidate_optimisation_is_exact():
    # the candidate-pruned BM25 must equal the full-scan BM25Index on every query.
    corpus = R.build_corpus("base")
    retrieve = R._bm25_retriever(corpus)
    for q in R.build_queries("base", max_events=140):
        opt = retrieve(q, 10)
        full = [d for d, _ in corpus.bm25.rank_with_scores("", 10, query_tokens=q.lex)]
        assert opt == full, f"candidate/full BM25 disagree on {q.event_id}"


@real_data
def test_no_query_leaks_an_answer_id():
    # HONESTY: a query token must never equal its event's radar id / event id, and
    # must never contain the ':'-joined event id. Query is built from operator
    # attributes only, so this holds by construction — assert it anyway.
    for e in R.load_events():
        q = R.Query(e, lambda t: t)
        toks = set(q.lex) | q.countries | q.asns | q.hints
        assert str(e.get("radar_event_id", "")).lower() not in toks
        assert e["event_id"] not in toks
        assert e["event_id"].split(":")[1] not in (set(q.lex) - q.asns)  # number only ok as an ASN


@real_data
def test_document_text_excludes_label_fields():
    # HONESTY: corpus text must not carry candidate_event_id / evidence_id, and the
    # event number must appear only as an entityless entity_id (which no query emits).
    corpus = R.build_corpus("base")
    for rec in R.load_evidence()[:2500]:
        toks = set(corpus.doc_tokens[R._doc_id(rec["evidence_id"])])
        assert rec["candidate_event_id"] not in toks
        assert rec["evidence_id"] not in toks
        num = rec["candidate_event_id"].split(":")[1]
        if num in toks:
            assert str(rec["entity_id"]) == num       # only via entityless entity_id


@real_data
def test_eval_metrics_bounded_and_never_beat_oracle():
    res = R.run_ioda_retrieval_eval("base", max_events=200)
    for method in res["methods"]:
        for k in res["k_values"]:
            row = res["methods"][method][k]
            assert 0.0 <= row["recall_at_k"] <= 1.0
            assert 0.0 <= row["false_retrieval"] <= 1.0
            # no retriever can exceed the size-capped oracle ceiling.
            assert row["recall_at_k"] <= res["oracle_recall_at_k"][k] + 1e-9


@real_data
def test_eval_is_deterministic():
    assert R.run_ioda_retrieval_eval("base", max_events=120) == \
           R.run_ioda_retrieval_eval("base", max_events=120)


@real_data
def test_time_window_is_the_lever_and_lexical_ties():
    # The honest headline: (1) structured (entity+time) hugely beats BM25;
    # (2) structured WITHOUT time ties the lexical baselines; (3) naive ≈ bm25.
    res = R.run_ioda_retrieval_eval("base", max_events=200)
    r = lambda m: res["methods"][m][10]["recall_at_k"]
    assert r("structured") > r("bm25") + 0.30            # time window is decisive
    assert r("structured_no_time") <= r("bm25") + 0.05   # entity-only doesn't beat lexical
    assert abs(r("naive") - r("bm25")) < 0.12            # lexical methods tie
    assert r("bm25") < 0.40                              # lexical stays weak


@real_data
def test_full_base_run_reproduces_reported_table():
    # Pins the actual reported numbers (deterministic) so the write-up cannot drift.
    res = R.run_ioda_retrieval_eval("base")
    assert res["n_queries"] == 832 and res["n_corpus_docs"] == 8542
    r = lambda m: res["methods"][m][10]["recall_at_k"]
    assert 0.85 <= res["oracle_recall_at_k"][10] <= 0.87
    assert 0.20 <= r("naive") <= 0.24
    assert 0.24 <= r("bm25") <= 0.29
    assert 0.73 <= r("structured") <= 0.77
    assert 0.59 <= r("rrf") <= 0.64
    assert 0.19 <= r("structured_no_time") <= 0.24
    # honest ordering: structured > rrf > bm25 > naive ≈ structured_no_time.
    assert r("structured") > r("rrf") > r("bm25") > r("naive")


@real_data
def test_query_expansion_is_a_null_op_here():
    # base/stem/expand are identical: country codes / ASN numbers have no morphology,
    # and outage words match nothing. Structured is mode-invariant by construction.
    runs = {m: R.run_ioda_retrieval_eval(m, max_events=150) for m in ("base", "stem", "expand")}
    for m in ("naive", "bm25", "structured", "rrf"):
        vals = {mode: runs[mode]["methods"][m][10]["recall_at_k"] for mode in runs}
        assert len(set(vals.values())) == 1, f"{m} changed across modes: {vals}"


@real_data
def test_crag_gate_summary_partitions_and_abstains():
    s = R.crag_gate_summary("base", max_events=120)
    assert sum(s["actions"].values()) == s["n_queries"] == 120
    # abstention (not_observed) fires on events BM25 cannot ground (entityless /
    # entity-mismatch) — the model-free "降级为未观测" path.
    assert s["reasons"].get("not_observed", 0) >= 1
    assert s["actions"]["correct"] >= 1


@real_data
def test_dataset_stats_report_reality():
    st = R.dataset_stats()
    assert st["n_events"] == 832
    assert st["n_corpus_docs"] == 8542
    assert st["events_with_ge1_evidence"] == 832 and st["events_with_0_evidence"] == 0
    s = st["sources_per_event"]
    assert s["single_source"] + s["two_source"] + s["three_source"] == 832
    assert s["three_source"] == 416
    assert st["event_class"] == {"CF-only": 259, "CF-IODA-overlap": 573}
    assert 9.0 <= st["relevant_set_size"]["mean"] <= 11.0
    assert st["locations_shared_by_multiple_events"] >= 1   # why entity-only over-retrieves
    assert 0.80 <= st["pool_time_in_window"]["frac"] < 1.0  # strong but not an oracle
