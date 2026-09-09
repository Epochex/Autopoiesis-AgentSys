import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.remediate.change_control import ChangeError, ChangeService, ChangeStore, digest
from frontend.gateway.app.device_changes import FortiAdapter


class Device:
    def __init__(self):
        self.value = {'mac': 'old'}
        self.writes = []
        self.healthy = True
        self.fail_apply = False
        self.foreign_value = False

    def identify(self):
        return {'serial': 'TEST-DEVICE', 'version': '1'}

    def prepare(self, action, target, params):
        return {'operations': [{'key': 'ip:1', 'before': copy.deepcopy(self.value), 'after': {'mac': 'new'}}],
                'observation_samples': 3, 'observation_interval': 0, 'manual_restore': True}

    def read(self, op):
        return copy.deepcopy(self.value)

    def apply(self, op, value):
        self.value = copy.deepcopy(value)
        self.writes.append(value)
        if self.fail_apply and value == {'mac': 'new'}:
            raise TimeoutError('reply_lost_after_device_commit')
        return {'accepted': True}

    def observe(self, plan):
        if self.foreign_value and self.writes:
            self.value = {'mac': 'another_operator'}
        return {'management_ok': True, 'guard_ok': self.healthy or not self.writes,
                'target_ok': self.healthy, 'scope': 'isolated test'}


@pytest.fixture
def setup(tmp_path):
    device = Device()
    store = ChangeStore(tmp_path / 'changes.sqlite')
    svc = ChangeService(store, lambda _: device, sleep=lambda _: None)
    plan = svc.prepare('test', 'assign', {'ip': 'test'}, {}, 'alice', 'replace device')
    return device, store, svc, plan


def approve(svc, plan):
    return svc.approve(plan['id'], plan['digest'], 'alice', 'verified target and diff')


def test_approved_write_channel_does_not_resume_legacy_automation(tmp_path, monkeypatch):
    from core.remediate.safety import EmergencyStop
    from frontend.gateway.app import change_routes, remediation
    approved = EmergencyStop(tmp_path / 'approved.json')
    legacy = EmergencyStop(tmp_path / 'legacy.json')
    monkeypatch.setattr(change_routes, 'write_control', lambda: approved)
    monkeypatch.setattr(remediation, 'emergency_stop', lambda: legacy)
    assert change_routes.writes_paused()
    approved.resume('tester', 'controlled change approval')
    assert not change_routes.writes_paused()
    assert legacy.paused and not legacy.path.exists()
    legacy.pause('explicit global stop', 'tester')
    assert change_routes.writes_paused()


def test_effect_probe_for_another_target_cannot_validate_this_change():
    adapter = FortiAdapter(object(), {'business_checks': [{'host': '192.168.1.1', 'port': 443}],
        'effect_checks': {'firewall_policy': [{'target': {'policy_id': '10'}, 'params': {},
            'checks': [{'host': '192.168.1.20', 'port': 443, 'reachable': True}]}]}})
    with pytest.raises(ChangeError, match='exact_target_effect_checks'):
        adapter.prepare('firewall_policy', {'policy_id': '11'}, {})


def test_preview_does_not_write_and_approval_binds_exact_diff(setup):
    device, store, svc, p = setup
    assert device.writes == []
    with pytest.raises(ChangeError, match='approval_required'):
        svc.execute(p['id'], 'alice')
    with pytest.raises(ChangeError, match='digest'):
        svc.approve(p['id'], 'wrong', 'alice', 'approve')
    approve(svc, p)
    result = svc.execute(p['id'], 'alice')
    assert result['status'] == 'succeeded'
    assert len([e for e in result['events'] if e['kind'] == 'business_readback']) == 3
    svc.execute(p['id'], 'alice')
    assert len(device.writes) == 1
    assert ChangeStore(store.path).get(p['id'])['status'] == 'succeeded'


def test_external_configuration_change_invalidates_approval(setup):
    device, store, svc, p = setup
    approve(svc, p)
    device.value = {'mac': 'external'}
    result = svc.execute(p['id'], 'alice')
    assert result['status'] == 'needs_human'
    assert not device.writes


def test_lost_reply_restores_actual_device_state(setup):
    device, store, svc, p = setup
    approve(svc, p)
    device.fail_apply = True
    result = svc.execute(p['id'], 'alice')
    assert result['status'] == 'rolled_back'
    assert device.value == {'mac': 'old'}
    assert [e['kind'] for e in result['events']].index('operation_started') < [e['kind'] for e in result['events']].index('restore_started')


def test_failed_business_check_restores_and_reports_unhealthy_recovery(setup):
    device, store, svc, p = setup
    approve(svc, p)
    device.healthy = False
    result = svc.execute(p['id'], 'alice')
    assert result['status'] == 'needs_human'
    assert device.value == {'mac': 'old'}
    assert result['recovery_errors']


def test_rollback_never_overwrites_another_operators_change(setup):
    device, store, svc, p = setup
    approve(svc, p)
    device.foreign_value = True
    result = svc.execute(p['id'], 'alice')
    assert result['status'] == 'needs_human'
    assert device.value == {'mac': 'another_operator'}
    assert len(device.writes) == 1


def test_explicit_restore_and_temporary_expiry(setup):
    device, store, svc, p = setup
    approve(svc, p)
    svc.execute(p['id'], 'alice')
    store.transition(p['id'], ('succeeded',), 'succeeded', {'restore_at': time.time() - 1})
    svc.expire_temporary_changes()
    assert store.get(p['id'])['status'] == 'restored'
    assert device.value == {'mac': 'old'}


def test_restart_reconciliation_retains_device_lock(setup):
    device, store, svc, p = setup
    approve(svc, p)
    store.transition(p['id'], ('approved',), 'executing', lease=True)
    svc.reconcile_interrupted()
    assert store.get(p['id'])['status'] == 'needs_human'
    other = svc.prepare('test', 'assign', {'ip': 'test'}, {}, 'alice', 'second')
    approve(svc, other)
    with pytest.raises(ChangeError, match='unreconciled'):
        svc.execute(other['id'], 'alice')
    assert not device.writes


def test_expired_plan_and_pause_never_write(setup):
    device, store, svc, p = setup
    approve(svc, p)
    svc.paused = lambda: True
    with pytest.raises(ChangeError, match='paused'):
        svc.execute(p['id'], 'alice')
    svc.paused = lambda: False
    store.transition(p['id'], ('approved',), 'approved', {'expires_at': time.time() - 1})
    with pytest.raises(ChangeError, match='expired'):
        svc.execute(p['id'], 'alice')
    assert not device.writes


def test_exact_preauthorization_with_durable_use_limit(setup):
    device, store, svc, p = setup
    rule = {'id': 'one', 'device_id': 'test', 'action': 'assign', 'target': p['target'], 'params': {},
            'plan_digest': digest(p['plan']), 'expires_at': time.time()+60, 'max_uses': 1}
    svc.preauthorize(p['id'], [rule])
    assert store.get(p['id'])['approval']['actor'] == 'preauthorization:one'
    other = svc.prepare('test', 'assign', p['target'], {}, 'alice', 'new case')
    with pytest.raises(ChangeError, match='no_matching'):
        svc.preauthorize(other['id'], [rule])


class FortiFake:
    def __init__(self):
        self.records = {
            '/api/v2/cmdb/system.dhcp/server': [{'id': 9, 'interface': 'lan', 'status': 'enable', 'default-gateway': '192.168.1.1', 'netmask': '255.255.255.0', 'ip-range': [{'start-ip': '192.168.1.2', 'end-ip': '192.168.1.254'}]}],
            '/api/v2/cmdb/system.dhcp/server/9/reserved-address': [],
            '/api/v2/monitor/system/dhcp': [{'ip': '192.168.1.110', 'mac': '00:11:22:33:44:66'}],
        }
        self.requests = []

    def request(self, method, path, body=None):
        path = path.split('?')[0]
        self.requests.append((method, path, body))
        if path == '/api/v2/monitor/system/status':
            return {'serial': 'FG-TEST', 'version': '6.2.7', 'results': {}}
        if method == 'GET':
            return {'status': 'success', 'results': copy.deepcopy(self.records.get(path, []))}
        if method == 'POST':
            key = str(body.get('id') or body.get('policyid'))
            self.records[path + '/' + key] = [dict(body, q_origin_key=key)]
        elif method == 'PUT':
            self.records[path] = [dict(self.records[path][0], **body)]
        elif method == 'DELETE':
            self.records.pop(path, None)
        return {'status': 'success', 'revision': 'test'}


def test_dhcp_plan_uses_real_server_and_targeted_create_restore(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOPOIESIS_OWNERSHIP_DB', str(tmp_path / 'unused.sqlite'))
    from domains.network_rca import environment
    monkeypatch.setattr(environment, 'load_arp_snapshot', lambda: [])
    transport = FortiFake()
    adapter = FortiAdapter(transport, {'business_checks': [{'host': '192.168.1.1', 'port': 443}]})
    p = adapter.prepare('dhcp_reserve', {'ip': '192.168.1.110', 'mac': '00:11:22:33:44:66'}, {})
    op = p['operations'][0]
    assert op['before'] is None
    assert op['resource'].endswith('/9/reserved-address/1')
    assert all(method == 'GET' for method, _, _ in transport.requests)
    adapter.apply(op, op['after'])
    assert adapter.read(op) == op['after']
    adapter.apply(op, None)
    assert adapter.read(op) is None


def test_device_protected_targets_and_missing_switch_are_rejected():
    transport = FortiFake()
    adapter = FortiAdapter(transport, {'business_checks': [{'host': '192.168.1.1', 'port': 443}]})
    with pytest.raises(ChangeError, match='protected'):
        adapter.prepare('dhcp_unreserve', {'ip': '192.168.1.1'}, {})
    with pytest.raises(ChangeError, match='not_found'):
        adapter.prepare('port_disable', {'switch': 'NONEXISTENT', 'port': 'port1'}, {})
    with pytest.raises(ChangeError, match='unexpected'):
        adapter.prepare('arp_clear', {'ip': '192.168.1.110', 'interface': 'lan', 'command': 'anything'}, {})
    assert all(m == 'GET' for m, _, _ in transport.requests)


def test_repeated_concurrent_execute_has_one_commit(setup):
    device, store, svc, p = setup
    approve(svc, p)
    def invoke():
        try:
            return svc.execute(p['id'], 'alice')['status']
        except ChangeError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert 'succeeded' in results
    assert len(device.writes) == 1


def test_operator_abort_recovers_before_another_observation(setup):
    device, store, svc, p = setup
    approve(svc, p)
    def abort(_):
        store.transition(p['id'], ('observing',), 'observing', {'abort_requested': True})
    svc.sleep = abort
    assert svc.execute(p['id'], 'alice')['status'] == 'rolled_back'
    assert device.value == {'mac': 'old'}


def test_fortiswitch_cycle_uses_two_bounded_operations_and_restores_baseline():
    transport = FortiFake()
    path = '/api/v2/cmdb/switch-controller/managed-switch/FS-TEST/ports/port3'
    transport.records[path] = [{'status': 'up', 'type': 'physical', 'poe-status': 'enable', 'poe-capable': 1}]
    adapter = FortiAdapter(transport, {'access_ports': [{'switch': 'FS-TEST', 'port': 'port3'}],
                                     'business_checks': [{'host': '192.168.1.1', 'port': 443}]})
    plan = adapter.prepare('poe_cycle', {'switch': 'FS-TEST', 'port': 'port3'}, {})
    assert [op['after'] for op in plan['operations']] == [{'poe-status': 'disable'}, {'poe-status': 'enable'}]
    for op in plan['operations']:
        assert adapter.read(op) == op['before']
        adapter.apply(op, op['after'])
    assert transport.records[path][0]['poe-status'] == 'enable'
    transport.records[path][0]['type'] = 'trunk'
    with pytest.raises(ChangeError, match='protected'):
        adapter.prepare('port_disable', {'switch': 'FS-TEST', 'port': 'port3'}, {})


def test_passwordless_console_keeps_csrf_and_diff_confirmation(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from frontend.gateway.app import change_routes as routes
    device, store, svc, p = setup
    monkeypatch.setattr(routes, '_service', svc)
    monkeypatch.setattr(routes, '_creds', lambda: ('https://router', 'admin', 'test-only-secret'))
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app, base_url='https://localhost')
    assert client.get('/api/rca/changes').status_code == 200
    assert client.post('/api/rca/changes/' + p['id'] + '/approve', json={'digest': p['digest'], 'reason': 'checked'}).status_code == 403
    client.headers['X-Autopoiesis-Console'] = 'change-control'
    assert client.post('/api/rca/changes/' + p['id'] + '/approve', headers={'Origin': 'https://untrusted.example'}, json={'digest': p['digest'], 'reason': 'checked'}).status_code == 403
    approved = client.post('/api/rca/changes/' + p['id'] + '/approve', json={'digest': p['digest'], 'reason': 'checked'})
    assert approved.status_code == 200
    assert approved.json()['approval']['actor'] == 'console:testclient'
    assert 'test-only-secret' not in str(store.get(p['id']))
    assert not any(route.path in ('/api/rca/changes/login', '/api/rca/changes/logout', '/api/rca/changes/session') for route in routes.router.routes)


def test_host_netplan_requires_registered_interface_and_independent_management():
    from frontend.gateway.app.host_changes import HostAdapter
    adapter = HostAdapter({'id': 'test', 'interfaces': ['eth9'], 'business_checks': [{'host': '192.168.1.1', 'port': 443}]})
    adapter.read = lambda _: {'dhcp4': True}
    with pytest.raises(ChangeError, match='independent'):
        adapter.prepare('host_static_ip', {'device': 'test', 'interface': 'eth9'}, {'address': '192.168.1.100/24', 'gateway': '192.168.1.1', 'dns': ['192.168.1.1']})
    with pytest.raises(ChangeError, match='allowlist'):
        adapter.prepare('host_static_ip', {'device': 'test', 'interface': 'eth0'}, {})
