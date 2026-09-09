from frontend.gateway.ingest.flow_ingest import parse_flow, parse_stream_event


def test_parse_goflow_record_into_shared_fact_row():
    row = parse_flow({
        "type": "IPFIX", "time_received_ns": 1_789_000_000_123_000_000,
        "sampler_address": "192.168.1.1", "src_addr": "192.168.1.110",
        "dst_addr": "1.1.1.1", "src_port": 51000, "dst_port": 443,
        "proto": "TCP", "bytes": 8192, "packets": 7, "in_if": 3, "out_if": 5,
        "src_mac": "E4:24:6C:E0:79:5E", "dst_mac": "00:11:22:33:44:55",
        "vlan_id": 16,
    })
    assert row is not None
    assert row[2:8] == ["192.168.1.1", "192.168.1.110", "1.1.1.1", 443, "TCP", "observed"]
    assert row[10:14] == ["traffic", "ipfix", "3", "5"]
    assert row[16:] == [8192, 0]
    assert len(row[1]) == 64


def test_parse_flow_rejects_missing_identity_or_timestamp():
    assert parse_flow({"src_addr": "192.168.1.2", "dst_addr": "bad", "time_received_ns": 1}) is None
    assert parse_flow({"src_addr": "192.168.1.2", "dst_addr": "192.168.1.3"}) is None


def test_flow_detector_envelope_preserves_relationship_and_volume():
    record = {
        "type": "IPFIX", "time_received_ns": 1_789_000_000_123_000_000,
        "sampler_address": "192.168.1.1", "src_addr": "192.168.1.110",
        "dst_addr": "1.1.1.1", "src_port": 51000, "dst_port": 443,
        "proto": "TCP", "bytes": 8192, "packets": 7, "in_if": 3, "out_if": 5,
        "src_mac": "E4:24:6C:E0:79:5E", "dst_mac": "00:11:22:33:44:55",
        "vlan_id": 16,
    }
    row = parse_flow(record)
    assert row is not None

    event = parse_stream_event(record, row)

    assert event["event_id"] == row[1]
    assert event["source_kind"] == "real"
    assert event["src_device_key"] == "e4:24:6c:e0:79:5e"
    assert event["srcip"] == "192.168.1.110"
    assert event["dstip"] == "1.1.1.1"
    assert event["srcport"] == 51000
    assert event["dstport"] == 443
    assert event["bytes_total"] == 8192
    assert event["pkts_total"] == 7
    assert event["dstmac"] == "00:11:22:33:44:55"
    assert event["vlan"] == 16
