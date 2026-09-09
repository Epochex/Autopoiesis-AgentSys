from datetime import datetime, timedelta, timezone

import pytest

from domains.network_rca.ownership_decisions import (
    assess_assignment, current_state, read_decisions, save_decision,
)
from domains.network_rca.environment import detect_l2_ownership_drift
from frontend.gateway.app import ownership

A = '00:11:22:33:44:55'
B = '00:11:22:33:44:66'
C = '00:11:22:33:44:77'
IP = '192.168.1.110'


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOPOIESIS_OWNERSHIP_DB', str(tmp_path / 'ownership.sqlite3'))


def test_decision_transfer_is_durable_versioned_and_preserves_history():
    save_decision(IP, A, 'set_owner', 0, 'asset label checked', 'operator')
    with pytest.raises(ValueError, match='revision_conflict'):
        save_decision(IP, B, 'set_owner', 0, 'stale screen', 'operator')
    receipt = save_decision(IP, B, 'set_owner', 1, 'replacement device', 'operator')
    assert receipt['previous_mac'] == A
    assert receipt['device_configuration_changed'] is False
    assert current_state(read_decisions(IP))['expected_mac'] == B
    assert len(read_decisions(IP)) == 2


def test_device_label_preserves_assignment_and_observation_boundary():
    save_decision(IP, A, 'set_owner', 0, 'checked', 'operator')
    before = current_state(read_decisions(IP))
    save_decision(IP, B, 'label_device', 1, 'label inspected', 'operator', '会议室打印机')
    after = current_state(read_decisions(IP))
    assert after['expected_mac'] == A
    assert after['decided_at'] == before['decided_at']
    assert after['retired_macs'] == []
    assert after['device_names'][B] == '会议室打印机'
    with pytest.raises(ValueError, match='device_name_required'):
        save_decision(IP, B, 'label_device', 2, 'checked', 'operator', ' ')


def test_retiring_record_does_not_suppress_new_observations():
    save_decision(IP, A, 'set_owner', 0, 'confirmed', 'operator')
    save_decision(IP, B, 'retire_record', 1, 'old equipment', 'operator')
    state = current_state(read_decisions(IP))
    stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    assert assess_assignment(state, [B], stamp, False) == 'conflict_observed'
    assert assess_assignment(state, [C], stamp, False) == 'conflict_observed'
    assert assess_assignment(state, [A], stamp, False) == 'matches_latest_observation'
    assert assess_assignment(state, [A], stamp, True) == 'awaiting_observation'
    assert assess_assignment(state, [A], state['decided_at'], False) == 'awaiting_observation'
    assert assess_assignment(state, [], stamp, False) == 'awaiting_observation'
    with pytest.raises(ValueError, match='clear_assignment_before_retiring'):
        save_decision(IP, A, 'retire_record', 2, 'bad request', 'operator')
    save_decision(IP, A, 'clear_owner', 2, 'reconsider', 'operator')
    assert current_state(read_decisions(IP))['expected_mac'] is None


def test_timeline_profiles_keep_all_claimants():
    history = [{'captured_at': f'2026-09-07T12:0{i}:00Z', 'records': [{'ip': IP, 'mac': mac, 'interface': 'lan'}]}
               for i, mac in enumerate([A, B, C, A])]
    finding = detect_l2_ownership_drift({'dhcp_ack': {}}, history)[0]
    assert len(finding['measured']['claimants']) == 3
    assert finding['measured']['claimants'][0]['samples'] == 2
    assert finding['measured']['claimants'][0]['last_seen'] == history[-1]['captured_at']


def test_identity_matches_mac_only_and_assignment_survives_closed_finding(monkeypatch):
    monkeypatch.setattr(ownership, 'load_arp_snapshot', lambda: [{'ip': IP, 'mac': B}])
    monkeypatch.setattr(ownership, 'arp_snapshot_provenance', lambda: {'captured_at': '2026-09-07T12:00:00Z', 'stale': False})
    report = {'findings': [{'subject': IP, 'fault_class': 'duplicate_ip_static', 'measured': {'macs': [A, B]}}]}
    ids = {'fresh': True, 'byMac': {A: {'hostname': 'Camera A'}}, 'byIp': {IP: {'hostname': 'WRONG NAME'}}}
    view = ownership.ownership_view(IP, report, ids)
    assert view['claimants'][0]['name'] == 'Camera A'
    assert view['claimants'][1]['name'] is None
    save_decision(IP, A, 'set_owner', 0, 'checked', 'operator')
    assert ownership.ownership_view(IP, {}, ids)['state']['expected_mac'] == A


def test_environment_reopens_assignment_mismatch_even_after_old_finding_resolved(monkeypatch):
    from domains.network_rca import environment as env
    save_decision(IP, A, 'set_owner', 0, 'verified asset', 'operator')
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    monkeypatch.setattr(env, 'sweep_corpus', lambda _: {'dhcp_ack': {}, 'sources_seen': set(), 'corpus': {}, 'leased_ips': set(), 'served_segments': set()})
    monkeypatch.setattr(env, 'load_arp_snapshot', lambda _: [{'ip': IP, 'mac': C}])
    monkeypatch.setattr(env, 'arp_snapshot_provenance', lambda _: {'captured_at': now.isoformat(), 'age_seconds': 0, 'stale': False})
    monkeypatch.setattr(env, 'load_l2_history', lambda _: [])
    monkeypatch.setattr(env, 'DETECTORS', ())
    monkeypatch.setattr(env, 'detect_identity_contradiction', lambda *args: [])
    monkeypatch.setattr(env, 'verify_findings', lambda *args, **kwargs: ([], [{'subject': IP, 'fault_class': 'duplicate_ip_static'}]))
    monkeypatch.setattr(env, 'sensor_coverage', lambda *args: [])
    monkeypatch.setattr(env, 'source_registry', lambda *args, **kwargs: [])
    monkeypatch.setattr(env, 'address_space', lambda *args: [])
    report = env.build_environment_report(flow_store={}, now=now)
    assert report['resolved'] == []
    finding = report['findings'][0]
    assert finding['detector'] == 'operator_assignment_mismatch'
    assert finding['verification']['state'] == 'confirmed'
    assert finding['measured']['operator_assignment']['expected_mac'] == A
    assert C in finding['measured']['macs']
