"""Environment memory keeps real host observations dated and replay-safe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.evolve import memory_ops
from core.memory.store import TieredMemoryStore
from domains.network_rca.environment_memory import observe_environment


LINKS = """\
lo               UNKNOWN        00:00:00:00:00:00
eth0             DOWN           00:11:22:33:44:50 <NO-CARRIER,BROADCAST,MULTICAST,UP>
eth1             DOWN           00:11:22:33:44:51 <NO-CARRIER,BROADCAST,MULTICAST,UP>
eth2             UP             00:11:22:33:44:52 <BROADCAST,MULTICAST,UP,LOWER_UP>
eth3             DOWN           00:11:22:33:44:53 <NO-CARRIER,BROADCAST,MULTICAST,UP>
eth4             DOWN           00:11:22:33:44:54 <NO-CARRIER,BROADCAST,MULTICAST,UP>
eth5             DOWN           00:11:22:33:44:55 <NO-CARRIER,BROADCAST,MULTICAST,UP>
docker0          DOWN           02:42:ac:11:00:01 <NO-CARRIER,BROADCAST,MULTICAST,UP>
br-a17           DOWN           02:42:aa:bb:cc:dd <NO-CARRIER,BROADCAST,MULTICAST,UP>
vethbeef@if7     UP             9a:00:00:00:00:01 <BROADCAST,MULTICAST,UP,LOWER_UP>
flannel.1        UNKNOWN        9a:00:00:00:00:02 <BROADCAST,MULTICAST,UP,LOWER_UP>
cni0             UP             9a:00:00:00:00:03 <BROADCAST,MULTICAST,UP,LOWER_UP>
idrac            DOWN           00:11:22:33:44:99 <BROADCAST,MULTICAST>
"""

ADDRESSES = """\
lo               UNKNOWN        127.0.0.1/8 ::1/128
eth0             DOWN
eth1             DOWN
eth2             UP             192.168.1.27/24 fe80::211:22ff:fe33:4452/64
eth3             DOWN
eth4             DOWN
eth5             DOWN
docker0          DOWN           172.17.0.1/16
br-a17           DOWN           172.18.0.1/16
vethbeef@if7     UP             fe80::9800:ff:fe00:1/64
flannel.1        UNKNOWN        10.42.0.0/32
cni0             UP             10.42.0.1/24
idrac            DOWN
"""


class FakeCommand:
    def __init__(self, links: str = LINKS, addresses: str = ADDRESSES, failed: str = ""):
        self.outputs = {
            ("ip", "-br", "link", "show"): links,
            ("ip", "-br", "addr", "show"): addresses,
            ("systemctl", "--failed", "--no-legend", "--plain"): failed,
        }

    def run(self, argv: list[str]):
        return SimpleNamespace(stdout=self.outputs[tuple(argv)], returncode=0)


def test_readings_cover_the_handwritten_environment_content(monkeypatch):
    calls: list[tuple[str, str, str, datetime]] = []

    def capture(_memory, *, subject, relation, value, observed_at, recorder=None):
        calls.append((subject, relation, value, observed_at))
        return f"{subject}:{relation}"

    monkeypatch.setattr(memory_ops, "observe_fact", capture, raising=False)
    observed = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    observe_environment(FakeCommand(), TieredMemoryStore(), now=observed)
    facts = {(asset, key): value for asset, key, value, _at in calls}

    # These are observed facts equivalent to four parts of KNOWN_NORMAL.  The
    # module records the environment shape; diagnosis decides what it means.
    assert facts[("eth0", "link_state")] == "no-carrier"
    assert facts[("eth2", "link_state")] == "up"
    assert facts[("eth2", "address")] == "192.168.1.27/24"
    assert facts[("this-host", "primary_interface")] == "eth2"
    assert facts[("docker0", "interface_kind")] == "container_bridge"
    assert facts[("br-a17", "interface_kind")] == "container_bridge"
    assert facts[("vethbeef", "interface_kind")] == "container_peer"
    assert facts[("flannel.1", "interface_kind")] == "k3s_overlay"
    assert facts[("cni0", "interface_kind")] == "k3s_bridge"
    assert facts[("idrac", "link_state")] == "down"
    assert {at for *_fact, at in calls} == {observed}


def test_naive_observation_time_is_made_explicit_utc(monkeypatch):
    times: list[datetime] = []

    def capture(_memory, *, subject, relation, value, observed_at, recorder=None):
        times.append(observed_at)
        return f"{subject}:{relation}"

    monkeypatch.setattr(memory_ops, "observe_fact", capture, raising=False)
    observe_environment(
        FakeCommand(), TieredMemoryStore(), now=datetime(2026, 8, 22, 12, 0)
    )

    assert times and all(at.tzinfo == timezone.utc for at in times)


def test_failed_probe_cannot_turn_missing_output_into_a_fact(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def capture(_memory, *, subject, relation, value, observed_at, recorder=None):
        calls.append((subject, relation, value))
        return f"{subject}:{relation}"

    class FailedLinks(FakeCommand):
        def run(self, argv: list[str]):
            result = super().run(argv)
            if argv[:3] == ["ip", "-br", "link"]:
                result.returncode = 1
                result.stdout = ""
            return result

    monkeypatch.setattr(memory_ops, "observe_fact", capture, raising=False)
    observe_environment(FailedLinks(), TieredMemoryStore())

    assert not any(relation == "link_state" for _subject, relation, _value in calls)
    assert not any(relation == "primary_interface" for _subject, relation, _value in calls)


HAS_OBSERVE_FACT = callable(getattr(memory_ops, "observe_fact", None))
needs_observe_fact = pytest.mark.xfail(
    not HAS_OBSERVE_FACT,
    reason="core.evolve.memory_ops.observe_fact is being implemented separately",
    strict=False,
)


@needs_observe_fact
def test_fifty_identical_observations_do_not_grow_the_store():
    memory = TieredMemoryStore()
    command = FakeCommand()
    start = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    observe_environment(command, memory, now=start)
    count = len(memory.records())
    for tick in range(1, 50):
        observe_environment(command, memory, now=start + timedelta(seconds=15 * tick))

    assert len(memory.records()) == count
    assert all(record.tier == "asset_profile" for record in memory.records())
    assert all(record.last_observed_at == start + timedelta(seconds=15 * 49)
               for record in memory.active())


@needs_observe_fact
def test_changed_link_state_retires_old_value_and_keeps_as_of_history():
    memory = TieredMemoryStore()
    recorder: list[dict] = []
    before = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    changed = before + timedelta(minutes=1)
    observe_environment(FakeCommand(), memory, now=before, recorder=recorder)

    changed_links = LINKS.replace(
        "eth2             UP             00:11:22:33:44:52 <BROADCAST,MULTICAST,UP,LOWER_UP>",
        "eth2             DOWN           00:11:22:33:44:52 <NO-CARRIER,BROADCAST,MULTICAST,UP>",
    )
    observe_environment(
        FakeCommand(links=changed_links), memory, now=changed, recorder=recorder
    )

    versions = [
        record for record in memory.records()
        if "eth2" in record.asset_ids and "relation:link_state" in record.tags
    ]
    assert len(versions) == 2
    old = next(record for record in versions if "value:up" in record.tags)
    new = next(record for record in versions if "value:no-carrier" in record.tags)
    assert old.quarantined is False
    assert old.valid_to == changed
    assert new.valid_from == changed and new.valid_to is None
    assert any(
        event["op"] == "REVOKE"
        and event["memory_id"] == old.memory_id
        and event["target_id"] == new.memory_id
        for event in recorder
    )
    assert all(event["tier"] == "asset_profile" for event in recorder)

    historical = memory.retrieve(
        ["eth2", "link_state"], ["eth2"], limit_per_tier=20, as_of=before
    )["asset_profile"]
    current = memory.retrieve(
        ["eth2", "link_state"], ["eth2"], limit_per_tier=20, as_of=changed
    )["asset_profile"]
    assert old in historical and new not in historical
    assert new in current and old not in current


def test_module_declares_memory_is_never_preflight_evidence():
    from domains.network_rca import environment_memory

    boundary = environment_memory.__doc__ or ""
    assert "blast_radius.estimate()" in boundary
    assert "preflight" in boundary
    assert "must never read" in boundary
