from __future__ import annotations

from collections import Counter

from frontend.gateway.app import device_profile as profile_module
from frontend.gateway.app.device_profile import _FIELD, _peer_row


def test_inbound_peer_uses_source_country_and_outbound_uses_destination_country() -> None:
    match = _FIELD.search(
        'srcip=81.16.152.2 srcport=1 dstip=77.236.99.122 dstport=8 proto=1 '
        'action="accept" service="PING" dstcountry="France" '
        'srccountry="United Kingdom" sentbyte=64 rcvdbyte=64'
    )
    assert match is not None
    fields = match.groupdict()
    assert fields["src_country"] == "United Kingdom"
    assert fields["dst_country"] == "France"

    aggregate = {
        "hits": 1,
        "accept": 1,
        "deny": 0,
        "sent": 64,
        "rcvd": 64,
        "ports": Counter({8: 1}),
        "services": Counter({"PING": 1}),
        "src_country": fields["src_country"],
        "dst_country": fields["dst_country"],
    }
    assert _peer_row("81.16.152.2", aggregate, inbound=True)["country"] == "United Kingdom"
    assert _peer_row("77.236.99.122", aggregate, inbound=False)["country"] == "France"


def test_profile_returns_every_peer_newest_first(monkeypatch) -> None:
    pairs = {}
    for index in range(15):
        pairs[("192.168.1.36", f"203.0.113.{index + 1}")] = {
            "hits": index + 1,
            "accept": index + 1,
            "deny": 0,
            "sent": 100,
            "rcvd": 200,
            "ports": Counter({443: index + 1}),
            "services": Counter({"HTTPS": index + 1}),
            "src_country": None,
            "dst_country": "United States",
            "last_seen": f"2026-09-08T12:{index:02d}:00",
        }
    monkeypatch.setattr(profile_module, "_mine", lambda: {
        "pairs": pairs, "hours": {}, "span": {}, "port_use": {},
    })
    monkeypatch.setattr(profile_module, "_load_device_graphs", lambda: {})
    monkeypatch.setattr(profile_module, "_rdns", lambda _ips: {})
    from frontend.gateway.app import live_identity
    monkeypatch.setattr(live_identity, "live_flows", lambda _ip: None)
    monkeypatch.setattr(live_identity, "device_status", lambda _ip, _lang: None)

    result = profile_module.device_profile("192.168.1.36")

    assert len(result["outbound"]) == 15
    assert result["outbound"][0]["ip"] == "203.0.113.15"
    assert result["outbound"][-1]["ip"] == "203.0.113.1"
