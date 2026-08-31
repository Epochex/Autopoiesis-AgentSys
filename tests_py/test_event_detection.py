from domains.network_rca.event_detection import DetectionPolicy, EventDetector, EventQualityGate


def _event(index: int, **updates):
    event = {
        "event_id": f"event-{index}",
        "event_ts": f"2026-08-31T12:00:{index:02d}Z",
        "parse_status": "ok",
        "source_kind": "real",
        "type": "traffic",
        "subtype": "local",
        "action": "deny",
        "srcip": "203.0.113.8",
        "dstip": "192.168.1.1",
        "dstport": 443,
        "service": "HTTPS",
        "policyid": 0,
        "bytes_total": 100,
    }
    event.update(updates)
    return event


def test_gate_rejects_replay_and_duplicate_records():
    gate = EventQualityGate()
    accepted, reason = gate.evaluate(_event(1))
    assert (accepted, reason) == (True, "accepted")
    assert gate.evaluate(_event(1)) == (False, "duplicate_event_id")
    assert gate.evaluate(_event(2, source_kind="replay")) == (
        False,
        "source_kind_not_allowed",
    )


def test_deny_window_keeps_unrelated_flows_separate():
    detector = EventDetector(DetectionPolicy(deny_threshold=3, cooldown_seconds=60))
    assert detector.process(_event(1)) == []
    assert detector.process(_event(2, dstport=22)) == []
    assert detector.process(_event(3)) == []
    alerts = detector.process(_event(4))
    assert len(alerts) == 1
    assert alerts[0]["rule_id"] == "deny_burst_v2"
    assert alerts[0]["metrics"]["deny_count"] == 3
    assert alerts[0]["data_classification"] == "observed"


def test_alert_identity_is_stable_for_retry():
    policy = DetectionPolicy(deny_threshold=1, cooldown_seconds=0)
    first = EventDetector(policy).process(_event(1))[0]
    second = EventDetector(policy).process(_event(1))[0]
    assert first["alert_id"] == second["alert_id"]


def test_routine_lan_broadcast_deny_is_not_promoted_to_an_incident():
    detector = EventDetector(DetectionPolicy(deny_threshold=1, cooldown_seconds=0))

    assert detector.process(_event(
        1,
        srcip="192.168.16.130",
        dstip="255.255.255.255",
        dstport=22222,
        srcintfrole="lan",
        subtype="local",
    )) == []


def test_one_continuous_admin_auth_campaign_emits_one_critical_incident():
    detector = EventDetector(DetectionPolicy(
        auth_failure_threshold=3,
        auth_distinct_source_threshold=2,
        cooldown_seconds=0,
    ))
    events = [
        _event(
            index,
            type="event",
            subtype="system",
            action="login",
            event_status="failed",
            logdesc="Admin login failed",
            srcip=f"198.51.100.{index}",
            user="admin",
            device_key="FGT-1",
        )
        for index in range(1, 6)
    ]

    alerts = [alert for event in events for alert in detector.process(event)]

    assert len(alerts) == 1
    assert alerts[0]["rule_id"] == "admin_auth_attack_v1"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["src_device_key"] == "FGT-1"
    assert alerts[0]["metrics"]["distinct_sources"] == 3
