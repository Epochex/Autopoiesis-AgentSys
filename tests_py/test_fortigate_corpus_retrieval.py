"""Property tests for the large real-corpus FortiGate retrieval eval (LLM-free).

These lock the honest guarantees of ``core.eval.fortigate_corpus_retrieval``:
deterministic parsing, a large mined corpus of real distractors, the ev-* -> category
mapping being structural (not label-based), the anti-circularity property that no unit
text carries an ev-id or the case query, and recalls that are well-formed and stable.
"""
from __future__ import annotations

import json

import pytest

from core.eval import fortigate_corpus_retrieval as fc


def _real_corpus_fixtures_present() -> bool:
    has_syslog = any(fc.DEFAULT_SYSLOG_DIR.glob("*.log")) or any(fc.DEFAULT_SYSLOG_DIR.glob("*.log.gz"))
    return has_syslog and fc.DEFAULT_TRAIN.is_file() and fc.DEFAULT_HELDOUT.is_file()


_requires_real_corpus = pytest.mark.skipif(
    not _real_corpus_fixtures_present(),
    reason=(
        "real FortiGate corpus fixtures absent: requires real/syslog/*.log[.gz], "
        "real/train_cases.json, and real/heldout_cases.json"
    ),
)


# ── 1. deterministic parser ────────────────────────────────────────────────────
_ADMIN_LINE = (
    'Apr  8 00:01:06 _gateway date=2026-04-08 time=00:01:06 devname="DAHUA_FORTIGATE" '
    'logid="0100032002" type="event" subtype="system" level="alert" '
    'logdesc="Admin login failed" user="phorn.chayly" srcip=62.60.131.60 dstip=77.236.99.125 '
    'action="login" status="failed" reason="name_invalid" '
    'msg="Administrator phorn.chayly login failed from https(62.60.131.60) because of invalid user name"'
)
_DENY_LINE = (
    'Jun 16 00:00:27 _gateway type="traffic" subtype="local" logid="0001000014" '
    'srcip=192.168.16.56 dstport=48689 action="deny" policyid=0 service="udp/48689" app="udp/48689"'
)
_DVR_LINE = (
    'Jun 16 00:00:27 _gateway type="traffic" subtype="forward" srcip=192.168.1.20 '
    'dstport=37777 action="deny" service="Dahua SDK"'
)


def test_parser_extracts_fields_including_quoted_spaces():
    f = fc.parse_syslog_line(_ADMIN_LINE)
    assert f["logid"] == "0100032002"
    assert f["type"] == "event" and f["subtype"] == "system"
    assert f["logdesc"] == "Admin login failed"   # quoted value with a space, unquoted
    assert f["srcip"] == "62.60.131.60"            # unquoted value
    assert f["action"] == "login"
    assert "Administrator phorn.chayly login failed" in f["msg"]
    # the BSD syslog prefix ("Apr 8 ... _gateway") has no '=' and must be ignored
    assert "_gateway" not in f and "Apr" not in f


def test_parser_is_total_and_deterministic():
    assert fc.parse_syslog_line("") == {}
    assert fc.parse_syslog_line("no key value pairs here") == {}
    assert fc.parse_syslog_line(_DENY_LINE) == fc.parse_syslog_line(_DENY_LINE)


# ── 2. category classification is structural ────────────────────────────────────
def test_classify_uses_structured_fields():
    assert fc.classify_category(fc.parse_syslog_line(_ADMIN_LINE)) == "admin_login_failed"
    assert fc.classify_category(fc.parse_syslog_line(_DENY_LINE)) == "policy_deny"
    # DVR service port splits device-probe out of generic policy-deny
    assert fc.classify_category(fc.parse_syslog_line(_DVR_LINE)) == "device_port_probe"
    assert fc.classify_category({"action": "accept"}) == "traffic_accept"
    assert fc.classify_category({"logdesc": "session clash"}) == "session_clash"
    assert fc.classify_category({"foo": "bar"}) is None


# ── 3. mined corpus is large and two-tiered ─────────────────────────────────────
@pytest.fixture(scope="module")
def units():
    if not _real_corpus_fixtures_present():
        pytest.skip(
            "real FortiGate corpus fixtures absent: requires real/syslog/*.log[.gz], "
            "real/train_cases.json, and real/heldout_cases.json"
        )
    return fc.build_corpus()


def test_corpus_is_large_with_summary_and_entity_tiers(units):
    assert len(units) > 500                                  # large real distractor pool
    tiers = {u.tier for u in units.values()}
    assert tiers == {"summary", "entity"}
    # exactly one summary unit per signal category actually present
    cats = {u.category for u in units.values()}
    summaries = {u.category for u in units.values() if u.tier == "summary"}
    assert summaries == cats
    # the shattering really happened for the high-cardinality categories
    admin_entities = [u for u in units.values() if u.category == "admin_login_failed" and u.tier == "entity"]
    assert len(admin_entities) > 100


# ── 4. ev-* -> category mapping is complete and structural (no label leakage) ────
def test_every_required_ev_maps_to_present_categories(units):
    cases = fc.load_cases()
    assert len(cases) == 8
    present = {u.category for u in units.values()}
    for c in cases:
        for ev in c["required"]:
            cats = fc.EV_TO_CATEGORY.get(ev)
            assert cats, f"{ev} unmapped"
            assert cats & present, f"{ev} maps to no category present in the corpus"
            # the mapping resolves to at least one real gold unit
            assert fc.gold_units_for_ev(ev, units)["summary"], f"{ev} has no summary target"


def test_gold_resolution_is_structural_not_by_label(units):
    # gold membership depends only on a unit's category, never on the ev string appearing
    # in the unit text -> the ev-id cannot be what links query to evidence.
    gold = fc.gold_units_for_ev("ev-admin-auth-failures", units)["all"]
    assert gold, "expected admin-failure units"
    for uid in gold:
        assert units[uid].category == "admin_login_failed"


# ── 5. ANTI-CIRCULARITY: no unit carries an ev-id or the case query text ─────────
def test_no_unit_text_contains_ev_ids_or_case_queries(units):
    cases = fc.load_cases()
    queries = [c["query"].lower() for c in cases]
    for u in units.values():
        blob = (u.text + " " + " ".join(u.tags)).lower()
        assert "ev-" not in blob, f"unit {u.unit_id} leaks an ev-id: {blob!r}"
        # a whole case query must never appear verbatim inside a unit's searchable text
        for q in queries:
            assert q not in blob


def test_unit_text_is_built_from_real_log_vocabulary(units):
    # the admin-fail summary must describe itself with the device's own logdesc words,
    # which is exactly the honest lexical bridge to the operator query.
    summ = units[fc._summary_id("admin_login_failed")]
    assert "admin" in summ.text and "login" in summ.text and "failed" in summ.text


# ── 6. eval is well-formed, bounded and deterministic ───────────────────────────
@_requires_real_corpus
def test_eval_shapes_and_bounds():
    res = fc.run_corpus_retrieval_eval(mode="base")
    assert res["n_cases"] == 8
    assert res["corpus_size"] > 500
    assert res["signal_categories"] >= 8          # few, distinct signal categories (type saturation)
    assert res["canonical_targets"] >= 8
    assert res["canonical_non_targets"] == res["corpus_size"] - res["canonical_targets"]
    for method in ("naive", "bm25", "structured", "rrf"):
        for k in res["k_values"]:
            for metric in ("coverage_recall", "canonical_recall"):
                v = res["methods"][method][k][metric]
                assert 0.0 <= v <= 1.0
    # coverage is never below canonical (any-unit is a superset of summary-only)
    for method in ("naive", "bm25", "structured", "rrf"):
        for k in res["k_values"]:
            assert res["methods"][method][k]["coverage_recall"] + 1e-9 >= \
                   res["methods"][method][k]["canonical_recall"]


@_requires_real_corpus
def test_eval_is_deterministic():
    a = fc.run_corpus_retrieval_eval(mode="base")
    b = fc.run_corpus_retrieval_eval(mode="base")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@_requires_real_corpus
def test_rrf_not_worse_than_bm25_on_canonical_headline():
    res = fc.run_corpus_retrieval_eval(mode="base")
    k = fc._HEADLINE_K
    assert res["methods"]["rrf"][k]["canonical_recall"] >= res["methods"]["bm25"][k]["canonical_recall"]


# ── 7. a known query retrieves its category; CRAG gate is wired in ──────────────
def test_admin_query_surfaces_admin_category(units):
    retrieve = fc.build_retrievers(units, mode="base")["bm25"]
    top = [uid for uid, _ in retrieve("admin authentication login failures", 10)]
    assert any(units[uid].category == "admin_login_failed" for uid in top)


def test_crag_gate_demo_returns_decision():
    dec = fc.demo_crag_gate("admin login failed from external ip", hi=3.0, lo=0.5)
    assert dec.action in ("correct", "ambiguous", "incorrect")
