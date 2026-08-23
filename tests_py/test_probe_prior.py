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
    assert opened["trace_events"] == []


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
    assert opened["trace_events"] == []
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
    assert opened["trace_events"] == []


def test_probe_prior_path_never_builds_or_calls_an_llm(monkeypatch):
    _fake_commands(monkeypatch, disk_use=30)
    monkeypatch.setattr(investigate, "_live_memory_store", lambda: _disk_memory())
    monkeypatch.setattr(
        investigate,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("start must not construct an LLM client")),
    )

    assert investigate.start("disk pressure on this host")["evidence"]
