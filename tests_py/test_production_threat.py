from copy import deepcopy

from frontend.gateway.app import production_threat as threat


def observation():
    return {
        'observedAt': '2026-09-08T08:00:00Z',
        'externalSources': [
            {'ip': '87.251.64.127', 'events': 1343, 'eventTypes': ['admin_login_failed']},
            {'ip': '87.251.64.128', 'events': 1342},
        ],
        'inventory': {'assets': [{'ip': '192.168.1.10'}, {'ip': '192.168.1.20'}]},
        'internetOutbound': {'records': [{'source': '192.168.1.10', 'destination': '87.251.64.127'}]},
    }


def setup_model(monkeypatch, output):
    calls = []
    class Client:
        def complete_json(self, messages, **kwargs):
            calls.append(messages)
            return deepcopy(output)
    monkeypatch.setattr(threat, 'cache_get', lambda *a, **k: None)
    monkeypatch.setattr(threat, 'cache_put', lambda *a, **k: None)
    monkeypatch.setattr(threat, 'client_for', lambda *a, **k: Client())
    monkeypatch.setattr(threat, 'model_name', lambda: 'test-model')
    return calls


def test_analysis_preserves_production_counts_and_filters_unsupported_links(monkeypatch):
    calls = setup_model(monkeypatch, {
        'verdict': 'credential attack', 'severity': 'high', 'confidence': 1.5,
        'siblings': [{'ip': '87.251.64.128'}, {'ip': '1.2.3.4'}],
        'internalCorrelation': [{'ip': '192.168.1.10', 'relation': 'observed outbound'}, {'ip': '192.168.1.20'}],
        'impactNodes': ['192.168.1.10', '1.2.3.4'],
        'playbook': [{'targetIp': '192.168.1.10', 'commands': ['ss -nt'], 'preconditions': 'host access'}],
    })
    result = threat.assess_production_source('87.251.64.127', observation())
    assert result['ok'] and len(calls) == 1
    assert result['eventCount'] == 1343 and 'attempts' not in result and 'lockouts' not in result
    assert result['observedAt'] == observation()['observedAt']
    assert [r['ip'] for r in result['siblings']] == ['87.251.64.128']
    assert [r['ip'] for r in result['internalCorrelation']] == ['192.168.1.10']
    assert result['impactNodes'] == ['192.168.1.10']
    assert result['playbook'][0]['preconditions'] == 'host access'
    assert result['confidence'] == 1
    assert '1343' in calls[0][1]['content']


def test_missing_source_does_not_call_model(monkeypatch):
    calls = setup_model(monkeypatch, {})
    assert not threat.assess_production_source('1.2.3.4', observation())['ok']
    assert not calls


def test_disabled_model_surfaces_error(monkeypatch):
    setup_model(monkeypatch, {})
    monkeypatch.setattr(threat, 'client_for', lambda *a, **k: None)
    monkeypatch.setattr(threat, 'unavailable', lambda *a: {'ok': False, 'text': 'disabled'})
    assert threat.assess_production_source('87.251.64.127', observation()) == {'ok': False, 'text': 'disabled'}


def test_malformed_model_collections_do_not_crash(monkeypatch):
    setup_model(monkeypatch, {'siblings': 'bad', 'playbook': [None, {'commands': 'bad'}], 'actions': {}, 'confidence': 'high'})
    result = threat.assess_production_source('87.251.64.127', observation())
    assert result['siblings'] == [] and result['playbook'] == [] and result['confidence'] is None


def test_host_and_subnet_analysis_use_current_assets_and_observed_edges(monkeypatch):
    evidence = observation()
    evidence['inventory']['assets'][0]['segment'] = '192.168.1.0/24'
    calls = setup_model(monkeypatch, {
        'analysis': 'current', 'impactPeers': [{'ip': '87.251.64.127'}, {'ip': '1.2.3.4'}],
        'patterns': [{'title': 'egress', 'members': ['192.168.1.10', '1.2.3.4']}],
        'corridors': [{'src': '192.168.1.10', 'dst': '87.251.64.127'}, {'src': '192.168.1.10', 'dst': '1.2.3.4'}],
    })
    host = threat.assess_production_scope('192.168.1.10', 'host', evidence)
    subnet = threat.assess_production_scope('192.168.1.0/24', 'subnet', evidence)
    assert host['ok'] and subnet['ok'] and len(calls) == 2
    assert [row['ip'] for row in host['impactPeers']] == ['87.251.64.127']
    assert subnet['patterns'][0]['members'] == ['192.168.1.10']
    assert len(subnet['corridors']) == 1
    assert not threat.assess_production_scope('unknown', 'host', evidence)['ok']


def test_playbook_runs_only_on_managed_targets_not_external_attack_origins(monkeypatch):
    monkeypatch.setenv('AUTOPOIESIS_FGT_BASE', 'https://192.168.1.1')
    calls = setup_model(monkeypatch, {'playbook': [
        {'targetIp': '87.251.64.127', 'commands': ['systemctl stop ssh']},
        {'targetIp': '87.251.64.0/24', 'commands': ['iptables -A INPUT -j DROP']},
        {'targetIp': '192.168.1.1', 'commands': ['get system status']},
    ]})
    result = threat.assess_production_source('87.251.64.127', observation())
    assert [step['targetIp'] for step in result['playbook']] == ['192.168.1.1']
    assert 'FortiOS' in calls[0][1]['content']


def test_command_blocks_are_preserved_without_truncating_closing_lines(monkeypatch):
    commands = ['config firewall address'] + ['    # review'] * 12 + ['end']
    setup_model(monkeypatch, {'playbook': [{'targetIp': '192.168.1.10', 'commands': commands}]})
    result = threat.assess_production_source('87.251.64.127', observation())
    assert result['playbook'][0]['commands'] == commands
