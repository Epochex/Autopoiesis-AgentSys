"""Memory may choose where triage looks first, never what current state means.

These tests keep command execution deterministic and offline.  The procedural
record supplies a probe prefix, the semantic record supplies the same historical
root key, and only the fake command's fresh output may confirm that root.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.memory.store import MemoryRecord, TieredMemoryStore
from frontend.gateway.app import investigate


class _Execution:
    def __init__(self, command: str, output: str):
        self.command = command
        self.output = output

    def as_evidence(self, evidence_id: str) -> dict:
        return {
            "evidence_id": evidence_id,
            "command": self.command,
            "output": self.output,
            "ok": True,
            "exit_code": 0,
        }


def _outputs(*, disk_use: int = 30) -> dict[str, str]:
    return {
        "hostname": "probe-host\n",
        "uptime": "up 2 days\n",
        "ip -br addr show": "eth2 UP 192.168.1.27/24\n",
        "ip -br link show": "eth2 UP\neth0 DOWN NO-CARRIER\n",
        "ip route show": "default via 192.168.1.1 dev eth2\n192.168.1.0/24 dev eth2\n",
        "ip neigh show": "192.168.1.1 dev eth2 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n",
        "systemctl --failed --no-legend": "",
        "df -h": f"Filesystem Size Used Avail Use% Mounted on\n/dev/sda 100G {disk_use}G 10G {disk_use}% /\n",
        "free -m": "Mem: 1000 200 100 0 100 700\n",
        "ss -tulpn": "tcp LISTEN 0 128 127.0.0.1:8026 0.0.0.0:*\n",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz": "200",
        "journalctl -p err -n 40 --no-pager --since -24h": "-- No entries --\n",
        "dmesg -T --level err,crit,alert -x": "",
    }


def _fake_commands(monkeypatch, *, disk_use: int = 30) -> list[str]:
    called: list[str] = []
    outputs = _outputs(disk_use=disk_use)

    def execute(command: str) -> _Execution:
        called.append(command)
        return _Execution(command, outputs.get(command, "ok\n"))

    monkeypatch.setattr(investigate, "run", execute)
    return called


def _disk_memory(*, observed_at: datetime | None = None, every_probe: bool = False) -> TieredMemoryStore:
    observed_at = observed_at or datetime.now(timezone.utc)
    probe_tags = (
        [f"probe:{command}" for command in investigate.TRIAGE_PROBES]
        if every_probe
        else ["probe:df -h", "skill:disk_usage"]
    )
    memory = TieredMemoryStore()
    memory.add(MemoryRecord(
        memory_id="proc-disk-pressure",
        tier="procedural",
        text="disk pressure: inspect disk usage first",
        tags=["disk", "pressure", "root:disk_pressure", *probe_tags],
        confidence=1.8,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    memory.add(MemoryRecord(
        memory_id="sem-disk-pressure",
        tier="semantic",
        text="recurring disk pressure pattern",
        tags=["disk", "pressure", "root:disk_pressure"],
        confidence=1.4,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    return memory


def _failed_service_memory() -> TieredMemoryStore:
    observed_at = datetime.now(timezone.utc)
    memory = TieredMemoryStore()
    memory.add(MemoryRecord(
        memory_id="proc-sentinel.failed_units",
        tier="procedural",
        text="for failed units, inspect failed services first",
        tags=[
            "service", "failed", "demo-collector.service",
            "root:sentinel.failed_units", "skill:failed_services",
        ],
        asset_ids=["demo-collector.service"],
        confidence=1.8,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    memory.add(MemoryRecord(
        memory_id="sem-sentinel.failed_units",
        tier="semantic",
        text="recurring failed systemd unit pattern",
        tags=["service", "failed", "root:sentinel.failed_units"],
        asset_ids=["demo-collector.service"],
        confidence=1.4,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    ))
    return memory


def test_without_relevant_memory_order_and_count_are_exactly_unchanged(monkeypatch):
    called = _fake_commands(monkeypatch)
    memory = TieredMemoryStore()
    memory.add(MemoryRecord(
        memory_id="proc-unrelated",
        tier="procedural",
        text="unrelated authentication history",
        tags=["authentication", "root:admin_lockout", "skill:check_admin_lockout"],
        last_observed_at=datetime.now(timezone.utc),
    ))
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: memory)

    opened = investigate.start("disk pressure on this host")

    expected = [*investigate.BASELINE_PROBES, *investigate.TRIAGE_PROBES]
    assert called == expected
    assert opened["probe_candidates"] == expected
    assert len(opened["evidence"]) == len(expected)
    assert not any(row["kind"] == "memory_shortcut" for row in opened["trace_events"])
    retrieval = next(row for row in opened["trace_events"] if row["kind"] == "memory_candidates_ranked")
    assert retrieval["payload"]["returned_count"] == 0


def test_memory_reorders_triage_without_changing_its_candidate_set(monkeypatch):
    called = _fake_commands(monkeypatch, disk_use=30)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: _disk_memory())

    opened = investigate.start("disk pressure on this host")
    triage_candidates = opened["probe_candidates"][len(investigate.BASELINE_PROBES):]

    assert triage_candidates[0] == "df -h"
    assert set(triage_candidates) == set(investigate.TRIAGE_PROBES)
    assert len(triage_candidates) == len(investigate.TRIAGE_PROBES)
    assert called == opened["probe_candidates"]
    trace = opened["trace_events"][0]["payload"]
    assert trace["effect"] == "probe_order"
    assert trace["saved_probe_count"] == 0
    assert trace["memory_ids"] == ["proc-disk-pressure", "sem-disk-pressure"]


def test_fresh_matching_root_stops_the_tail_and_records_exact_savings(monkeypatch):
    called = _fake_commands(monkeypatch, disk_use=95)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: _disk_memory())

    opened = investigate.start("disk pressure on this host")

    assert called == [*investigate.BASELINE_PROBES, "df -h"]
    assert set(opened["probe_candidates"][len(investigate.BASELINE_PROBES):]) == set(
        investigate.TRIAGE_PROBES
    )
    payload = opened["trace_events"][0]["payload"]
    assert payload["effect"] == "probe_order_and_early_stop"
    assert payload["confirmed_root_key"] == "disk_pressure"
    assert payload["saved_probe_count"] == len(investigate.TRIAGE_PROBES) - 1
    assert payload["saved_probe_count"] == len(payload["skipped_probes"])
    assert payload["memory_ids"] == ["proc-disk-pressure", "sem-disk-pressure"]


def test_failed_service_demo_combines_memory_shortcut_and_knowledge_grounding(monkeypatch):
    called: list[str] = []
    outputs = _outputs()
    outputs["systemctl --failed --no-legend"] = (
        "demo-collector.service loaded failed failed Demo collector\n"
    )

    def execute(command: str) -> _Execution:
        called.append(command)
        return _Execution(command, outputs.get(command, "ok\n"))

    monkeypatch.setattr(investigate, "run", execute)
    monkeypatch.setattr(investigate, "_live_memory_store", _failed_service_memory)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})

    opened = investigate.start(
        "demo-collector.service 服务失败，现在是什么情况",
        subject="demo-collector.service",
    )

    assert called == [*investigate.BASELINE_PROBES, "systemctl --failed --no-legend"]
    assert opened["knowledge_context"]
    assert opened["knowledge_context"][0]["document_id"] == "systemctl-failed-units"
    shortcut = next(row for row in opened["trace_events"] if row["kind"] == "memory_shortcut")
    assert shortcut["payload"]["effect"] == "probe_order_and_early_stop"
    assert shortcut["payload"]["confirmed_root_key"] == "sentinel.failed_units"
    assert shortcut["payload"]["saved_probe_count"] == len(investigate.TRIAGE_PROBES) - 1
    assert shortcut["payload"]["subject"] == "demo-collector.service"


def test_a_wrong_memory_guess_only_changes_order_and_runs_the_full_sweep(monkeypatch):
    called = _fake_commands(monkeypatch, disk_use=30)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: _disk_memory())

    opened = investigate.start("disk pressure on this host")

    assert len(called) == len(investigate.BASELINE_PROBES) + len(investigate.TRIAGE_PROBES)
    assert len(opened["evidence"]) == len(called)
    assert opened["trace_events"][0]["payload"]["saved_probe_count"] == 0


def test_memory_naming_the_entire_sweep_claims_no_shortcut(monkeypatch):
    called = _fake_commands(monkeypatch, disk_use=95)
    monkeypatch.setattr(
        investigate, "_live_memory_store", lambda: _disk_memory(every_probe=True)
    )

    opened = investigate.start("disk pressure on this host")

    assert called == [*investigate.BASELINE_PROBES, *investigate.TRIAGE_PROBES]
    assert not any(row["kind"] == "memory_shortcut" for row in opened["trace_events"])
    assert any(row["kind"] == "memory_candidates_ranked" for row in opened["trace_events"])
    assert opened["probe_prior"]["strictly_narrowed"] is False


def test_fully_stale_memory_stays_visible_but_cannot_reorder(monkeypatch):
    called = _fake_commands(monkeypatch, disk_use=95)
    # Past the procedural horizon (30 days), not five minutes: a learned method
    # does not rot in minutes. One global 60s horizon used to make every real
    # memory fully stale, which silently disabled this whole feature.
    stale_at = datetime.now(timezone.utc) - timedelta(days=60)
    monkeypatch.setattr(
        investigate, "_live_memory_store", lambda: _disk_memory(observed_at=stale_at)
    )

    opened = investigate.start("disk pressure on this host")

    assert called == [*investigate.BASELINE_PROBES, *investigate.TRIAGE_PROBES]
    considered = opened["probe_prior"]["considered"]
    assert {item["memory_id"] for item in considered} == {
        "proc-disk-pressure", "sem-disk-pressure",
    }
    assert all(item["staleness"] == 1.0 for item in considered)
    assert all(item["effective_confidence"] == 0.0 for item in considered)
    assert not any(row["kind"] == "memory_shortcut" for row in opened["trace_events"])
    assert any(row["kind"] == "memory_candidates_ranked" for row in opened["trace_events"])


def test_probe_prior_path_never_builds_or_calls_an_llm(monkeypatch):
    _fake_commands(monkeypatch, disk_use=30)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: _disk_memory())
    monkeypatch.setattr(
        investigate,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("start must not construct an LLM client")),
    )

    assert investigate.start("disk pressure on this host")["evidence"]


def test_investigation_saves_scored_cross_source_retrieval_receipt(monkeypatch):
    _fake_commands(monkeypatch)
    monkeypatch.setattr(investigate, "_live_memory_store", _failed_service_memory)
    monkeypatch.setattr(
        investigate,
        "_operational_context",
        lambda subject, family: {
            "historical_only": True,
            "dossiers": [{
                "dossier_id": "incident-service-001",
                "source_mode": "live",
                "fault_summary": "collector service stopped after dependency failure",
                "asset_ids": [subject],
                "fault_family": family,
            }],
            "risks": [],
            "features": [],
        },
    )

    opened = investigate.start(
        "demo-collector.service 服务失败",
        family="fam-perception-selfheal",
        subject="demo-collector.service",
    )

    results = opened["retrieval_results"]
    memory = next(item for item in results if item["item_id"] == "proc-sentinel.failed_units")
    dossier = next(item for item in results if item["item_id"] == "incident-service-001")
    knowledge = next(item for item in results if item["kind"] == "knowledge_document")

    assert memory["score"] > 0
    assert memory["score_components"]["asset_hits"] == 1
    assert memory["source"] == "tiered_memory_store"
    assert memory["relation_to_current"]["investigation_id"] == opened["session_id"]
    assert "subject" in memory["relation_to_current"]["matched_on"]
    assert dossier["score_type"] == "source_rank_reciprocal"
    assert dossier["relation_to_current"]["family"] == "fam-perception-selfheal"
    assert knowledge["score_type"] == "bm25"
    assert knowledge["locator"]

    receipt = next(
        row for row in opened["trace_events"] if row["kind"] == "memory_candidates_ranked"
    )
    assert "persistence_error" not in receipt
    assert receipt["payload"]["returned_count"] == len(results)
    assert receipt["payload"]["counts_by_kind"] == {
        "incident_dossier": 1,
        "indexed_memory": 2,
        "knowledge_document": len(opened["knowledge_context"]),
    }


def test_analyze_and_followup_receive_the_saved_retrieval_results(monkeypatch):
    _fake_commands(monkeypatch)
    monkeypatch.setattr(investigate, "_live_memory_store", _failed_service_memory)
    monkeypatch.setattr(investigate, "_operational_context", lambda *_args: {})
    opened = investigate.start(
        "demo-collector.service 服务失败",
        family="fam-perception-selfheal",
        subject="demo-collector.service",
    )
    client = type("Client", (), {
        "seen": [],
        "complete_json": lambda self, messages, schema_name: (
            self.seen.append(messages)
            or ({"diagnosis": "d", "root_cause": "x", "citations": [],
                 "need_commands": [], "runbook": []}
                if schema_name == "rca_analysis"
                else {"answer": "a", "citations": [], "need_commands": []})
        ),
    })()
    monkeypatch.setattr(investigate, "_client", lambda: client)

    investigate.analyze(opened["session_id"])
    investigate.ask(opened["session_id"], "历史线索是什么")

    assert "proc-sentinel.failed_units" in client.seen[0][-1]["content"]
    assert "tiered_memory_store" in client.seen[0][-1]["content"]
    assert "proc-sentinel.failed_units" in client.seen[1][-1]["content"]
