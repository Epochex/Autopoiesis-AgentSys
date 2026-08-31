from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domains.network_rca.incident_scope import derive_incident_scope


TOPOLOGY = {
    "subnets": [
        {"cidr": "192.168.1.0/24", "intf": "fortilink"},
        {"cidr": "192.168.16.0/24", "intf": "LACP"},
    ]
}


def test_local_in_scope_separates_external_actor_from_managed_target() -> None:
    observed = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    scope = derive_incident_scope(
        subject="8.8.8.8",
        service="tcp/5555",
        first_seen_at=observed.isoformat(),
        last_seen_at=observed.isoformat(),
        facts={
            "sourceIp": "8.8.8.8",
            "destinationIp": "203.0.113.10",
            "trafficSubtype": "local",
            "policyType": "local-in-policy",
            "sourceInterface": "wan1",
            "windowSeconds": 60,
            "observedAt": observed.isoformat(),
        },
        managed_gateway="192.168.1.1",
        fault_family="fam-policy-reachability",
        topology=TOPOLOGY,
    )

    assert scope.managed_assets == ("192.168.1.1",)
    assert scope.external_actors == ("8.8.8.8",)
    assert scope.fault_domain == "gateway-control-plane:192.168.1.1:wan1"
    assert scope.quality == "exact"
    start = datetime.fromisoformat(scope.incident_start)
    end = datetime.fromisoformat(scope.incident_end)
    assert timedelta(seconds=65) <= observed - start <= timedelta(seconds=70)
    assert timedelta(seconds=5) <= end - observed <= timedelta(seconds=10)


def test_forwarded_flow_uses_observed_interfaces_and_managed_segments() -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    scope = derive_incident_scope(
        subject="192.168.1.20",
        service="tcp/37777",
        first_seen_at=now.isoformat(),
        last_seen_at=now.isoformat(),
        facts={
            "sourceIp": "192.168.1.20",
            "destinationIp": "192.168.16.56",
            "sourceInterface": "fortilink",
            "destinationInterface": "LACP",
            "policyId": 17,
            "observedAt": now.isoformat(),
        },
        managed_gateway="192.168.1.1",
        fault_family="fam-policy-reachability",
        topology=TOPOLOGY,
    )

    assert scope.managed_assets == ("192.168.1.20", "192.168.16.56")
    assert scope.fault_domain == "forwarding-path:fortilink->LACP:policy-17"
    assert scope.external_actors == ()


def test_unmapped_public_record_has_no_probe_target() -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc).isoformat()
    scope = derive_incident_scope(
        subject="8.8.8.8",
        service=None,
        first_seen_at=now,
        last_seen_at=now,
        facts={"sourceIp": "8.8.8.8", "destinationIp": "1.1.1.1"},
        managed_gateway="192.168.1.1",
        topology=TOPOLOGY,
    )

    assert scope.quality == "unresolved"
    assert scope.managed_assets == ()
    assert "managedAsset" in scope.missing


def test_ipv6_local_in_scope_terminates_on_managed_gateway() -> None:
    now = datetime(2026, 8, 31, 7, 33, tzinfo=timezone.utc).isoformat()
    scope = derive_incident_scope(
        subject="fe80::19f6:8fb1:57ea:abd",
        service="udp/3702",
        first_seen_at=now,
        last_seen_at=now,
        facts={
            "sourceIp": "fe80::19f6:8fb1:57ea:abd",
            "destinationIp": "ff02::c",
            "trafficSubtype": "local",
            "policyType": "local-in-policy6",
            "sourceInterface": "port5",
            "sourceInterfaceRole": "lan",
            "action": "deny",
        },
        managed_gateway="192.168.1.1",
        fault_family="fam-policy-reachability",
        topology=TOPOLOGY,
    )

    assert scope.managed_assets == ("192.168.1.1",)
    assert scope.fault_domain == "gateway-control-plane:192.168.1.1:port5"
    assert scope.quality == "exact"
