"""The sentinel learns only from disposition chains proven by the watch window."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.memory.store import TieredMemoryStore
from core.skills.registry import SkillRegistry
from domains.network_rca.incident_memory import (
    consolidate_incident_timeline,
    synthesize_incident_run,
)


def _passed_chain() -> list[dict]:
    return [
        {
            "at": "2026-08-22T10:00:00+00:00",
            "kind": "detected",
            "detector": "failed_units",
            "family": "fam-perception-selfheal",
            "subject": "demo-api.service",
            "target": "demo-api.service",
            "severity": "high",
            "summary": "demo-api.service 挂了。",
            "evidence": {"line": "demo-api.service loaded failed failed"},
            "action": "restart_unit",
            "streak": 2,
        },
        {
            "at": "2026-08-22T10:00:01+00:00",
            "kind": "command",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "argv": ["systemctl", "show", "demo-api.service"],
            "rc": 0,
            "out": "ActiveState=failed",
            "truncated": False,
        },
        {
            "at": "2026-08-22T10:00:02+00:00",
            "kind": "preflight",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "eligible": True,
            "reason": "unit is failed and within its restart budget",
            "blast_radius": {"addresses": ["192.168.10.27"]},
        },
        {
            "at": "2026-08-22T10:00:03+00:00",
            "kind": "remediation_committed",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "baseline": {"unit:demo-api.service": False, "gateway": True},
        },
        {
            "at": "2026-08-22T10:00:04+00:00",
            "kind": "bakein_opened",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "window_seconds": 90,
            "probes": ["unit:demo-api.service", "gateway"],
        },
        {
            "at": "2026-08-22T10:00:19+00:00",
            "kind": "bakein_sampled",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "probe": "unit:demo-api.service",
            "reading": {"active": "active", "running": True},
            "healthy": True,
            "regressed": False,
        },
        {
            "at": "2026-08-22T10:01:34+00:00",
            "kind": "bakein_passed",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "samples": 12,
            "elapsed_seconds": 90,
        },
        {
            "at": "2026-08-22T10:01:35+00:00",
            "kind": "remediated",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "detector": "failed_units",
            "outcome": "passed",
            "needs_human": False,
            "samples": 12,
            "baseline": {"unit:demo-api.service": False, "gateway": True},
        },
        {
            "at": "2026-08-22T10:01:36+00:00",
            "kind": "resolved",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "detector": "failed_units",
            "outcome": "passed",
            "samples": 12,
        },
    ]


def _escalated_chain() -> list[dict]:
    return [
        deepcopy(_passed_chain()[0]),
        {
            "at": "2026-08-22T10:00:03+00:00",
            "kind": "escalated",
            "subject": "demo-api.service",
            "action": "restart_unit",
            "detector": "failed_units",
            "recurrences": 3,
            "window_hours": 24,
            "prior_cycles": [
                {"at": "2026-08-21T12:00:00+00:00", "outcome": "passed"},
                {"at": "2026-08-21T18:00:00+00:00", "outcome": "passed"},
                {"at": "2026-08-22T03:00:00+00:00", "outcome": "passed"},
            ],
            "reason": "restart_unit held briefly three times and the unit failed again",
        },
    ]


def test_passed_chain_uses_existing_consolidation_and_is_retrievable_in_english():
    memory = TieredMemoryStore()

    reports = consolidate_incident_timeline(_passed_chain(), memory, SkillRegistry())

    assert len(reports) == 1
    assert reports[0].passed is True
    assert {record.tier for record in memory.records()} == {
        "episodic",
        "semantic",
        "procedural",
    }
    episodic = next(record for record in memory.records() if record.tier == "episodic")
    assert "root:sentinel.failed_units" in episodic.tags
    assert "failed_units" in episodic.tags
    assert "demo-api.service" in episodic.asset_ids
    assert "192.168.10.27" in episodic.asset_ids
    assert all(item.get("evidence_id") for item in episodic.evidence_snapshot)
    assert "outcome:ineffective" not in episodic.tags
    assert all(not record.event_type.startswith("ineffective:") for record in memory.records())

    recalled = memory.retrieve(["failed_units"], [], limit_per_tier=3)
    assert episodic.memory_id in {record.memory_id for record in recalled["episodic"]}


def test_replaying_the_same_chain_does_not_add_or_reinforce_again():
    memory = TieredMemoryStore()
    chain = _passed_chain()
    first = consolidate_incident_timeline(chain, memory, SkillRegistry())
    before = [record.model_dump(mode="json") for record in memory.records()]

    second = consolidate_incident_timeline(deepcopy(chain), memory, SkillRegistry())

    assert first and second == []
    assert [record.model_dump(mode="json") for record in memory.records()] == before


def test_escalated_chain_creates_labelled_ineffective_memory_and_is_idempotent():
    memory = TieredMemoryStore()
    chain = _escalated_chain()

    first = consolidate_incident_timeline(chain, memory, SkillRegistry())
    before = [record.model_dump(mode="json") for record in memory.records()]
    second = consolidate_incident_timeline(deepcopy(chain), memory, SkillRegistry())

    assert len(first) == 1
    assert first[0].passed is False
    assert len(first[0].added) == 1
    assert second == []
    assert [record.model_dump(mode="json") for record in memory.records()] == before

    record = memory.records()[0]
    assert record.tier == "episodic"
    assert record.confidence == 1.0
    assert "outcome:ineffective" in record.tags
    assert "terminal:escalated" in record.tags
    assert "ineffective-key:sentinel.failed_units" in record.tags
    assert "action:restart_unit" in record.tags
    assert record.event_type == "ineffective:escalated"
    assert "action=restart_unit" in record.text
    assert "attempts=3" in record.text
    assert "failed again" in record.text
    assert "demo-api.service" in record.asset_ids

    recalled = memory.retrieve(["failed_units"], [], limit_per_tier=3)
    assert [item.memory_id for item in recalled["episodic"]] == [record.memory_id]


@pytest.mark.parametrize(
    ("events", "terminal", "attempts"),
    [
        (
            [{
                "at": "2026-08-22T10:00:03+00:00",
                "kind": "declined",
                "subject": "demo-api.service",
                "action": "restart_unit",
                "detector": "failed_units",
                "reason": "preflight rejected the action",
            }],
            "declined",
            0,
        ),
        (
            [
                {
                    "at": "2026-08-22T10:00:03+00:00",
                    "kind": "remediation_committed",
                    "subject": "demo-api.service",
                    "action": "restart_unit",
                },
                {
                    "at": "2026-08-22T10:00:18+00:00",
                    "kind": "bakein_regressed",
                    "subject": "demo-api.service",
                    "action": "restart_unit",
                    "probes": ["unit:demo-api.service"],
                },
                {
                    "at": "2026-08-22T10:00:19+00:00",
                    "kind": "revert_unverified",
                    "subject": "demo-api.service",
                    "action": "restart_unit",
                    "reason": "readback disagrees with baseline",
                },
            ],
            "revert_unverified",
            1,
        ),
    ],
)
def test_other_ineffective_terminals_record_reason_and_attempt_count(
    events: list[dict], terminal: str, attempts: int,
):
    memory = TieredMemoryStore()

    reports = consolidate_incident_timeline(
        [deepcopy(_passed_chain()[0]), *events], memory, SkillRegistry()
    )

    assert len(reports) == 1
    record = memory.records()[0]
    assert f"terminal:{terminal}" in record.tags
    assert f"outcome={terminal}" in record.text
    assert f"attempts={attempts}" in record.text
    assert "reason=" in record.text


def test_ineffective_memory_bypasses_positive_consolidation(monkeypatch):
    memory = TieredMemoryStore()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("ineffective memory entered positive consolidation")

    monkeypatch.setitem(
        consolidate_incident_timeline.__globals__,
        "consolidate_run",
        fail_if_called,
    )

    reports = consolidate_incident_timeline(
        _escalated_chain(), memory, SkillRegistry()
    )

    assert len(reports) == 1
    assert reports[0].passed is False
    assert len(memory.records()) == 1


def test_detailed_followup_requires_its_own_pass_boundary():
    chain = [event for event in _passed_chain() if event["kind"] != "bakein_passed"]
    assert synthesize_incident_run(chain) is None


def test_run_and_evidence_ids_are_stable_on_replay():
    first = synthesize_incident_run(_passed_chain())
    second = synthesize_incident_run(deepcopy(_passed_chain()))

    assert first is not None and second is not None
    assert first.run_id == second.run_id
    assert [item["evidence_id"] for item in first.evidence] == [
        item["evidence_id"] for item in second.evidence
    ]
    assert {
        "remediation_committed",
        "bakein_opened",
        "bakein_sampled",
        "bakein_passed",
    }.issubset({event.kind for event in first.events})


def test_absent_store_and_absent_dsn_are_silent(monkeypatch):
    assert consolidate_incident_timeline(_passed_chain(), None, SkillRegistry()) == []

    from frontend.gateway.app import sentinel_wiring

    monkeypatch.delenv("AUTOPOIESIS_MEMORY_DSN", raising=False)
    monkeypatch.delenv("SELFEVO_MEMORY_DSN", raising=False)
    sentinel_wiring._remember_completed_incidents()


def test_wiring_resolves_the_shared_store_when_a_later_poll_can_see_it(monkeypatch):
    from frontend.gateway.app import sentinel_wiring

    memory = TieredMemoryStore()
    service = SimpleNamespace(memory=memory, skills=SkillRegistry())
    monkeypatch.setattr(sentinel_wiring, "_resolve_learning_service", lambda: service)
    monkeypatch.setattr(sentinel_wiring, "timeline", lambda _limit: _passed_chain())

    sentinel_wiring._remember_completed_incidents()
    sentinel_wiring._remember_completed_incidents()

    assert len(memory.records()) == 3


def test_wiring_copies_watch_window_readbacks_into_the_sentinel_timeline(monkeypatch):
    from frontend.gateway.app import remediation, sentinel_wiring

    recorded: list[tuple[str, dict]] = []

    def execute(action, target, *, emit, on_command):
        emit("remediation_committed", {"action": f"{action}:{target}"})
        emit("bakein_sampled", {"probe": "gateway", "healthy": True})
        emit("bakein_passed", {"samples": 1})
        return {"verdict": {"outcome": "passed"}}

    monkeypatch.setattr(remediation, "execute", execute)
    monkeypatch.setattr(
        sentinel_wiring,
        "record",
        lambda kind, payload: recorded.append((kind, payload)),
    )

    sentinel = sentinel_wiring._build()
    sentinel.execute("restart_unit", "demo-api.service", on_command=lambda _row: None)

    assert [kind for kind, _payload in recorded] == [
        "remediation_committed",
        "bakein_sampled",
        "bakein_passed",
    ]
    assert all(payload["subject"] == "demo-api.service" for _kind, payload in recorded)
    assert all(payload["action"] == "restart_unit" for _kind, payload in recorded)
    assert recorded[0][1]["followup_action"] == "restart_unit:demo-api.service"


def test_a_single_retired_record_does_not_block_seeding(tmp_path, monkeypatch):
    """One quarantined row used to disable the domain priors forever.

    `records()` counts quarantined rows; `active()` does not. A store holding
    nothing but a retired record is empty for seeding purposes, and the failure
    was invisible: no error, no log line, just five priors that never loaded.
    """
    from core.memory.store import MemoryRecord, TieredMemoryStore
    from domains.network_rca.factory import load_memory_records

    store = TieredMemoryStore()
    store.add(MemoryRecord(memory_id="retired", tier="semantic", text="x"))
    store.quarantine("retired", "left over from a probe")
    assert store.records(), "the retired row is still there"
    assert not store.active(), "but nothing is live"

    # This is the condition the factory now uses.
    assert not store.active(), "so seeding must still be allowed to run"
    store.seed(load_memory_records())
    assert len(store.active()) == 5, "all five domain priors must load"


# ── tag harvesting: it used to take everything, which is three bugs at once ──

def test_harvested_tags_are_identifiers_not_debris():
    """One chain produced several hundred tags before this was bounded.

    Every ASCII run in every string in every event was kept AND split, so a
    single journal line contributed its every word and a timestamp contributed
    its every field. That drowns BM25, makes the exact-entity route meaningless,
    and is the widest possible injection surface for host-controlled text.
    """
    from domains.network_rca.incident_memory import _ascii_terms

    chain = [
        {"kind": "detected", "at": "2026-08-22T23:49:06.039836+00:00",
         "subject": "demo-collector.service", "detector": "failed_units",
         "action": "restart_unit", "summary": "demo-collector.service 挂了。",
         "evidence": {"line": "demo-collector.service loaded failed Demo for rehearsal"}},
        {"kind": "command", "at": "x", "rc": 0, "out": "200",
         "argv": ["curl", "-s", "-m", "5", "-o", "/dev/null",
                  "http://127.0.0.1:8026/api/healthz"]},
    ]
    tags = _ascii_terms(chain, "sentinel.failed_units")

    assert len(tags) <= 24, "an unbounded tag list is a blast radius, not a feature"
    assert "demo-collector.service" in tags, "the identifier that matters survives"
    assert "restart_unit" in tags
    # debris that used to be indexed
    for junk in ("2026", "08", "039836", "5", "loaded", "for", "and"):
        assert junk not in tags, f"{junk!r} is not an operational identifier"


def test_host_output_cannot_inject_a_reserved_tag():
    """`root:` and `skill:` are read as structured claims, not as words.

    A unit name is attacker-influencable in principle, and the identifier
    pattern allows ':'. Without this filter a unit called
    `root:carrier_down.service` writes a working root-cause assertion that
    `_remembered_root()` then reads as ground truth.
    """
    from domains.network_rca.incident_memory import _ascii_terms

    chain = [{"kind": "detected", "at": "x", "detector": "failed_units",
              "action": "restart_unit", "subject": "root:injected_fake.service",
              "evidence": {"line": "skill:pretend_probe quarantine:bogus"}}]
    tags = _ascii_terms(chain, "sentinel.failed_units")

    assert not any(t.startswith(("root:", "skill:", "quarantine:")) for t in tags)
