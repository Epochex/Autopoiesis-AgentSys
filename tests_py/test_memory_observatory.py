"""The memory observatory reports the REAL lifecycle — or admits it cannot.

This layer is pure observability: it must (a) surface what the kernel actually did,
down to the item level, and (b) never invent a value the kernel does not produce.
These tests guard both halves, plus the property that observing changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.evolve import compare_cold_vs_warm, consolidate_run, run_evolving_stream
from core.evolve.observatory import (
    CAPABILITIES,
    added,
    quarantine_reason,
    serialize_store,
    snapshot,
)
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from domains.network_rca.factory import load_ground_truth, load_seed_cases

RUN = "run-1"


@dataclass
class _Case:
    id: str
    query: str
    query_terms: list[str]
    assets: list[str]
    relevant_skills: list[str] = field(default_factory=list)


def _events(case: _Case, *, root: str, passed: bool = True, read: dict | None = None) -> list[TraceEvent]:
    """A minimal but REAL-shaped trace for one run: what consolidate_run consumes."""
    return [
        TraceEvent(run_id=RUN, case_id=case.id, kind="memory_read", payload=read or {"episodic": []}),
        TraceEvent(run_id=RUN, case_id=case.id, kind="skills_exposed", payload={"skills": []}),
        TraceEvent(run_id=RUN, case_id=case.id, kind="verifier_result", payload={"passed": passed}),
        TraceEvent(
            run_id=RUN, case_id=case.id, kind="diagnosis_completed",
            payload={"root_cause_key": root, "confidence": 0.95,
                     "evidence": [{"evidence_id": "ev-1"}]},
        ),
    ]


def _consolidate(case, mem, *, root, passed=True, read=None) -> list[dict]:
    ops: list[dict] = []
    consolidate_run(
        _events(case, root=root, passed=passed, read=read),
        case, mem, SkillRegistry(), [{"evidence_id": "ev-1", "source": "s", "summary": "obs"}],
        recorder=ops,
    )
    return ops


def _epi_ops(ops: list[dict]) -> list[dict]:
    return [o for o in ops if o["tier"] == "episodic" and o["op"] in ("ADD", "UPDATE", "NOOP")]


# ── the router's real decisions reach the UI, each keeping its own identity ───
def test_add_carries_the_real_route_similarity_and_no_fabricated_before():
    mem = TieredMemoryStore()
    case = _Case("c1", "carrier down", ["carrier", "down"], ["r230"])
    op = _epi_ops(_consolidate(case, mem, root="carrier_down"))[0]
    assert op["op"] == "ADD"
    assert op["similarity"] == 0.0          # real: nothing to compare against in an empty store
    assert op["before"] is None             # a new record has no prior state — not invented
    assert op["after"]["strength"] == 1.0
    assert op["added_tags"] == []           # no before => no honest diff to report


def test_update_reports_a_real_before_after_diff():
    mem = TieredMemoryStore()
    base = _Case("c1", "carrier down", ["carrier", "down"], ["r230"])
    _consolidate(base, mem, root="carrier_down")
    # a re-observed variant of the same family: same assets, one extra term
    variant = _Case("c2", "carrier down extra", ["carrier", "down", "extra"], ["r230"])
    op = _epi_ops(_consolidate(variant, mem, root="carrier_down"))[0]

    assert op["op"] == "UPDATE"
    assert 0.62 <= op["similarity"] < 0.97          # real score, inside the router's UPDATE band
    assert op["target_id"] is not None
    assert op["before"] != op["after"]             # the merge is visible, not aliased away
    assert op["before"]["confidence"] < op["after"]["confidence"]
    assert "extra" in op["added_tags"]             # real set difference, after minus before
    assert "extra" not in op["before"]["tags"]


def test_noop_stays_distinct_from_reinforce():
    """A NOOP is the router saying 'already captured' — collapsing it into REINFORCE
    would erase the one op that proves dedupe works."""
    mem = TieredMemoryStore()
    case = _Case("c1", "carrier down", ["carrier", "down"], ["r230"])
    _consolidate(case, mem, root="carrier_down")
    twin = _Case("c2", "carrier down", ["carrier", "down"], ["r230"])
    op = _epi_ops(_consolidate(twin, mem, root="carrier_down"))[0]

    assert op["op"] == "NOOP"
    assert op["similarity"] >= 0.97
    assert op["target_id"] is not None


def test_quarantine_reason_parses_from_the_real_tag():
    mem = TieredMemoryStore()
    mem.add(MemoryRecord(memory_id="m1", tier="episodic", text="prior", tags=["carrier"]))
    case = _Case("c1", "carrier down", ["carrier", "down"], ["r230"])
    ops = _consolidate(case, mem, root="carrier_down", passed=False, read={"episodic": ["m1"]})

    q = [o for o in ops if o["op"] == "QUARANTINE"]
    assert [o["memory_id"] for o in q] == ["m1"]
    assert q[0]["before"]["tags"] == ["carrier"]
    assert "quarantine:contradicted" in q[0]["added_tags"]
    assert quarantine_reason(mem.get("m1")) == "contradicted"
    assert quarantine_reason(MemoryRecord(memory_id="x", tier="episodic", text="t")) is None


def test_serialized_store_keeps_quarantined_records_with_their_reason():
    """The seed stream never rejects a run, so this path is proven directly:
    a quarantined record must survive into `records` (audit), flagged and explained."""
    mem = TieredMemoryStore()
    mem.add(MemoryRecord(memory_id="m1", tier="episodic", text="a prior belief", tags=["carrier"]))
    case = _Case("c1", "carrier down", ["carrier", "down"], ["r230"])
    _consolidate(case, mem, root="carrier_down", passed=False, read={"episodic": ["m1"]})

    rows = {r["memory_id"]: r for r in serialize_store(mem)}
    assert "m1" in rows, "active() hides quarantined records; records() must not"
    assert rows["m1"]["quarantined"] is True
    assert rows["m1"]["quarantine_reason"] == "contradicted"
    assert rows["m1"]["text"] == "a prior belief"


def test_evidence_snapshot_is_trimmed_without_inventing_fields():
    mem = TieredMemoryStore()
    mem.add(MemoryRecord(
        memory_id="m1", tier="episodic", text="t",
        evidence_snapshot=[{"evidence_id": "ev-1", "source": "syslog",
                            "summary": "x" * 500, "bulk": "dropped"}],
    ))
    item = serialize_store(mem)[0]["evidence_snapshot"][0]
    assert item["evidence_id"] == "ev-1" and item["source"] == "syslog"
    assert item["truncated"] is True and len(item["summary"]) == 240
    assert "bulk" not in item          # trimmed for size...
    assert item["summary"] == "x" * 240  # ...but what remains is verbatim, never paraphrased


def test_insight_reports_its_real_member_provenance():
    mem = TieredMemoryStore()
    for i, root in enumerate(("carrier_down", "link_flap")):
        case = _Case(f"c{i}", f"{root} on r230", [root, "r230"], ["r230"])
        ops = _consolidate(case, mem, root=root)
    insight = [o for o in ops if o["op"] == "INSIGHT"]
    assert insight, "a matured family must abstract upward"
    members = insight[0]["source_memory_ids"]
    assert len(members) >= 2
    assert all(mem.get(m) is not None for m in members)  # real ids, resolvable in the store


def test_reflection_refresh_of_an_existing_insight_is_recorded():
    """reflect() re-derives a live insight's importance on EVERY pass, but returns only
    NEWLY created ids — so this mutation used to reach the store with no event at all."""
    mem = TieredMemoryStore()
    for i, root in enumerate(("carrier_down", "link_flap")):
        case = _Case(f"c{i}", f"{root} on r230", [root, "r230"], ["r230"])
        ops = _consolidate(case, mem, root=root)
    assert [o for o in ops if o["op"] == "INSIGHT"], "family matures on this pass"

    # a third incident on the same asset grows the family: the r230 insight is
    # refreshed (importance re-derived, new root tagged), NOT created a second time
    case = _Case("c2", "power_loss on r230", ["power_loss", "r230"], ["r230"])
    ops = _consolidate(case, mem, root="power_loss")
    assert not [o for o in ops if o["op"] == "INSIGHT"], "no second insight for one family"

    refresh = [o for o in ops if o["op"] == "INSIGHT_REFRESH"]
    assert refresh, "the refresh branch of reflect() must emit its own op"
    op = refresh[0]
    assert op["memory_id"] == "insight-r230" and op["tier"] == "semantic"
    assert op["similarity"] is None, "no route() ran on the dedupe-by-id path"
    assert op["before"] != op["after"], "a refresh that changed nothing is not an event"
    # the emitted 'after' is the record's REAL state, not a reconstruction
    assert op["after"]["importance"] == mem.get("insight-r230").importance
    assert op["source_memory_ids"], "the refreshed family's members are real provenance"


def _last_after(events: list[dict]) -> dict[str, dict]:
    """Per memory_id, the `after` of the highest-seq op that carries a snapshot."""
    out: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e["seq"]):
        if e.get("after") is not None:
            out[e["memory_id"]] = e["after"]
    return out


def test_every_records_last_event_reconciles_with_the_final_store():
    """THE observatory invariant, over the real API run the UI consumes.

    If the last recorded `after` for a record disagrees with that record's final
    value, then some mutation reached the store without emitting an event — and a
    cursor-scoped UI would contradict the record list. This failed for exactly one
    record (insight-fortigate, importance 21.02 vs 50.9) before reflect()'s refresh
    branch was recorded.
    """
    from frontend.gateway.app.rca_reader import load_evolution

    payload = load_evolution(None, 4)
    if not payload.get("ready"):
        pytest.skip("no validated real held-out dataset present")
    obs = payload["observatory"]
    records = obs["records"]
    assert records and obs["events"]

    last = _last_after(obs["events"])
    for record in records:
        mid = record["memory_id"]
        assert mid in last, f"{mid} reached the final store with no snapshot-bearing event"
        after = last[mid]
        for field_ in ("importance", "confidence"):
            assert after[field_] == record[field_], (
                f"{mid}.{field_}: last event says {after[field_]}, store says {record[field_]}"
            )
        for field_ in ("tags", "links"):
            assert sorted(after[field_]) == sorted(record[field_]), (
                f"{mid}.{field_}: last event says {after[field_]}, store says {record[field_]}"
            )


# ── the honesty contract ─────────────────────────────────────────────────────
def test_snapshot_deep_copies_so_before_cannot_alias_after():
    rec = MemoryRecord(memory_id="m", tier="episodic", text="t", tags=["a"])
    before = snapshot(rec)
    rec.tags.append("b")           # apply_route mutates lists in place, exactly like this
    assert before["tags"] == ["a"]
    assert added(before, snapshot(rec), "tags") == ["b"]
    assert added(None, snapshot(rec), "tags") == []


def test_recorder_is_optional_and_changes_nothing():
    """Observability must not perturb the experiment it observes."""
    cases, gt = load_seed_cases(), load_ground_truth()
    watched = run_evolving_stream(cases, gt, passes=3, evolve=True)
    assert watched["per_event"] and "observatory" in watched
    baseline = {k: v for k, v in watched.items() if k != "observatory"}
    # the same run, re-executed, must agree on every pre-existing metric
    again = run_evolving_stream(cases, gt, passes=3, evolve=True)
    assert {k: v for k, v in again.items() if k != "observatory"} == baseline


def test_cold_runs_have_no_observatory_and_capabilities_stay_honest():
    cases, gt = load_seed_cases(), load_ground_truth()
    cold = run_evolving_stream(cases, gt, passes=2, evolve=False)
    assert "observatory" not in cold  # evolve=False has no memory lifecycle to report

    warm = run_evolving_stream(cases, gt, passes=2, evolve=True)["observatory"]
    assert warm["capabilities"] == CAPABILITIES
    # Lifecycle operations and structured context-drop provenance are wired; retrieval
    # score tracing and UPDATE text mutation remain deliberately absent.
    assert CAPABILITIES["decay_wired"] is False
    assert CAPABILITIES["eviction_wired"] is True
    assert CAPABILITIES["conflict_update_wired"] is True
    assert CAPABILITIES["retrieval_scores"] is False
    assert CAPABILITIES["context_drop_reason"] is True
    assert CAPABILITIES["update_text_mutation"] is False


def test_records_carry_real_text_and_include_quarantined():
    cases, gt = load_seed_cases(), load_ground_truth()
    obs = run_evolving_stream(cases, gt, passes=3, evolve=True)["observatory"]
    records = obs["records"]
    assert records
    # every serialized record must correspond to real, non-empty learned content
    assert all(r["text"].strip() for r in records)
    assert all(r["memory_id"] and r["tier"] for r in records)
    assert len({r["memory_id"] for r in records}) == len(records)
    assert any(r["tier"] == "episodic" for r in records)
    assert any(r["tier"] == "procedural" for r in records)
    # every record the op stream says was created must exist in the final store,
    # and active() would hide quarantined ones — records() must not.
    ids = {r["memory_id"] for r in records}
    created = {e["memory_id"] for e in obs["events"] if e["op"] in ("ADD", "INSIGHT")}
    assert created and created <= ids
    assert {e["memory_id"] for e in obs["events"] if e["op"] == "QUARANTINE"} <= ids


def test_recall_dropped_ids_are_derived_not_guessed():
    cases, gt = load_seed_cases(), load_ground_truth()
    obs = run_evolving_stream(cases, gt, passes=3, evolve=True)["observatory"]
    assert obs["recall"]
    for row in obs["recall"]:
        retrieved = {mid for ids in row["retrieved"].values() for mid in ids}
        included = set(row["included_memory_ids"])
        # dropped is exactly retrieved-minus-included: a derivation, reproducible here
        assert set(row["dropped_memory_ids"]) == retrieved - included
        assert included <= retrieved  # context can never include a memory recall never returned
        if row["resolved"]:
            assert row["resolved_memory_ids"]
    drops = [drop for row in obs["recall"] for drop in row["context_drops"]]
    assert drops
    assert all(drop["section"] and drop["reason"] == "section_budget" for drop in drops)


def test_evolution_events_are_ordered_and_attributed():
    cases, gt = load_seed_cases(), load_ground_truth()
    obs = run_evolving_stream(cases, gt, passes=3, evolve=True)["observatory"]
    events = obs["events"]
    assert [e["seq"] for e in events] == list(range(len(events)))
    ids = {c.id for c in cases}
    for e in events:
        assert e["case_id"] in ids and e["run_id"]      # every op traces to a real run
        assert e["op"] in {
            "ADD", "UPDATE", "NOOP", "REINFORCE", "QUARANTINE", "INSIGHT", "INSIGHT_REFRESH", "LINK",
        }
        if e["op"] == "REINFORCE":
            assert e["before"] != e["after"]            # a reinforcement that changed nothing is a bug
            assert e["similarity"] is None              # no route() ran — not invented


def test_observatory_is_lifted_to_the_top_level_of_the_api_payload():
    cases, gt = load_seed_cases(), load_ground_truth()
    res = compare_cold_vs_warm(cases, gt, passes=2)
    # the pre-existing contract the current UI depends on must survive untouched
    assert {"warm", "cold", "delta", "memory"} <= set(res)
    assert "observatory" in res["warm"]
    assert "observatory" not in res["cold"]
