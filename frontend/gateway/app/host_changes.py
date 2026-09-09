"""Registered Linux hosts: failed-unit recovery and timed Netplan transactions."""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import subprocess
import time

import yaml

from core.remediate.change_control import ChangeError
from domains.network_rca.remediation import Command, read_unit, read_interface, restart_unit, bounce_interface, assert_not_tailscale
from .device_changes import CATALOG, ip4

_CLOUD_FILE = '/etc/cloud/cloud.cfg.d/99-autopoiesis-network.cfg'


class HostAdapter:
    def __init__(self, policy):
        self.policy = policy
        self.command = Command(self.run)
        self.trial = None

    def run(self, argv):
        if self.policy.get('ssh'):
            host = self.policy['ssh']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+', host):
                raise ChangeError('invalid_registered_ssh_host')
            argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', host, shlex.join(argv)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)

    def output(self, argv):
        done = self.run(argv)
        if done.returncode:
            raise ChangeError('host_command_failed:' + argv[0])
        return done.stdout.strip()

    def identify(self):
        machine = self.output(['cat', '/etc/machine-id'])
        if not re.fullmatch(r'[0-9a-f]{32}', machine):
            raise ChangeError('invalid_host_identity')
        return {'machine_id': machine, 'hostname': self.output(['hostname'])}

    def _bus(self, path, method, signature=None, *args):
        interface = 'io.netplan.Netplan' if path == '/io/netplan/Netplan' else 'io.netplan.Netplan.Config'
        argv = ['busctl', 'call', 'io.netplan.Netplan', path, interface, method]
        if signature:
            argv += [signature, *args]
        output = self.output(argv)
        if output.startswith('b ') and output != 'b true':
            raise ChangeError('netplan_transaction_refused')
        return output

    def read(self, op):
        if op['kind'] == 'cloud_init':
            return json.loads(self.output(['python3', '-c',
                'import pathlib,json; p=pathlib.Path(' + repr(_CLOUD_FILE) + '); print(json.dumps(p.read_text() if p.exists() else None))']))
        if op['kind'] == 'unit':
            return {'active': read_unit(self.command, op['name'])['active']}
        if op['kind'] == 'interface':
            return {'state': read_interface(self.command, op['name'])['state']}
        return yaml.safe_load(self.output(['netplan', 'get', 'ethernets.' + op['name']]))

    def prepare(self, action, target, params):
        if action not in ('host_static_ip', 'host_persist_network', 'restart_unit', 'bounce_interface'):
            raise ChangeError('unsupported_action_for_host')
        _, required, allowed, _, reversible = CATALOG[action]
        if set(target) != set(required) or set(params) - set(allowed) or target['device'] != self.policy['id']:
            raise ChangeError('invalid_host_action_fields')
        name = target.get('unit') or target.get('interface')
        if not re.fullmatch(r'[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_-]+)*', name or ''):
            raise ChangeError('invalid_host_target')
        assert_not_tailscale(name)
        kind = 'unit' if action == 'restart_unit' else 'interface' if action == 'bounce_interface' else 'netplan'
        allowlist = 'units' if kind == 'unit' else 'interfaces'
        if name not in self.policy.get(allowlist, []):
            raise ChangeError('host_target_requires_explicit_inventory_allowlist')
        op = {'kind': kind, 'name': name, 'key': kind + ':' + name}
        before = self.read(op)
        op['before'] = before
        if kind == 'unit':
            if before['active'] != 'failed':
                raise ChangeError('unit_must_be_failed')
            op.update(after={'active': 'active'}, irreversible=True)
        elif kind == 'interface':
            if before['state'] not in ('DOWN', 'NO-CARRIER'):
                raise ChangeError('interface_must_be_down')
            op.update(after={'state': 'UP'}, irreversible=True)
        else:
            # An independent connection is required before changing host addressing.
            if not self.policy.get('out_of_band_verified'):
                raise ChangeError('independent_host_management_path_required')
            if not isinstance(before, dict) or before.get('match') or before.get('set-name'):
                raise ChangeError('explicit_simple_netplan_interface_required')
            self.output(['busctl', 'introspect', 'io.netplan.Netplan', '/io/netplan/Netplan', 'io.netplan.Netplan'])
            address = ipaddress.IPv4Interface(params['address'])
            ip4(str(address.ip))
            gateway = ip4(params['gateway'])
            if ipaddress.ip_address(gateway) not in address.network:
                raise ChangeError('gateway_outside_address_network')
            dns = params.get('dns', [])
            if not isinstance(dns, list) or not 1 <= len(dns) <= 4:
                raise ChangeError('one_to_four_dns_addresses_required')
            after = dict(before, dhcp4=False, addresses=[str(address)], nameservers={'addresses': [ip4(x) for x in dns]})
            after.pop('gateway4', None)
            after['routes'] = [r for r in before.get('routes', []) if r.get('to') not in ('default', '0.0.0.0/0')]
            after['routes'].append({'to': 'default', 'via': gateway})
            op['after'] = after
            if after == before:
                raise ChangeError('configuration_already_matches')
        checks = self.policy.get('business_checks', [])
        if not checks:
            raise ChangeError('host_business_checks_required')
        operations = [op]
        if action == 'host_persist_network':
            cloud = {'kind': 'cloud_init', 'name': _CLOUD_FILE, 'key': 'cloud-init-network',
                     'after': 'network: {config: disabled}\n'}
            cloud['before'] = self.read(cloud)
            if cloud['before'] != cloud['after']:
                operations.insert(0, cloud)
        return {'title': CATALOG[action][0], 'action': action, 'target': target, 'params': params,
                'operations': operations, 'business_checks': checks, 'observation_interval': 10,
                'observation_samples': 12, 'manual_restore': reversible,
                'verification_scope': '主机配置、服务状态与业务连通性；重启持久性需单独重启验收',
                'rollback': 'Netplan Try 240 秒自动恢复；业务检查通过后才确认配置' if kind == 'netplan' else '瞬时动作无法逆转，异常升级人工'}

    def apply(self, op, value):
        if op['kind'] == 'cloud_init':
            # Fixed file only. Argument contains JSON data, never interpolated shell code.
            script = ('import json,os,pathlib,sys,tempfile; p=pathlib.Path(' + repr(_CLOUD_FILE) + '); '
                      'v=json.loads(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); '
                      '\nif v is None:\n p.unlink(missing_ok=True)\n'
                      'else:\n fd,tmp=tempfile.mkstemp(dir=p.parent); os.fchmod(fd,0o600); '
                      'f=os.fdopen(fd,"w"); f.write(v); f.flush(); os.fsync(f.fileno()); f.close(); os.replace(tmp,p)\n')
            self.output(['python3', '-c', script, json.dumps(value)])
            return {'operation': 'cloud_init_network_config', 'path': _CLOUD_FILE}
        if op['kind'] == 'unit':
            ok = restart_unit(self.command, op['name'])
            return {'command': ['systemctl', 'restart', op['name']], 'immediate_ok': ok}
        if op['kind'] == 'interface':
            ok = bounce_interface(self.command, op['name'])
            return {'command': ['ip', 'link', 'set', op['name'], 'down/up'], 'immediate_ok': ok}
        if value == op['before'] and self.trial:
            self._bus(self.trial, 'Cancel')
            self.trial = None
            return {'operation': 'netplan_cancel_trial', 'at': time.time()}
        reply = self._bus('/io/netplan/Netplan', 'Config')
        path = json.loads(reply.split(' ', 1)[1])
        if not re.fullmatch(r'/io/netplan/Netplan/config/[A-Za-z0-9]+', path):
            raise ChangeError('invalid_netplan_config_object')
        self.trial = path
        try:
            # Null first removes obsolete keys, such as an old gateway4, in the isolated copy.
            self._bus(path, 'Set', 'ss', f'ethernets.{op["name"]}=null', '90-autopoiesis')
            self._bus(path, 'Set', 'ss', f'ethernets.{op["name"]}=' + json.dumps(value), '90-autopoiesis')
            self._bus(path, 'Try', 'u', '240')
        except Exception:
            try:
                self._bus(path, 'Cancel')
            finally:
                self.trial = None
            raise
        return {'operation': 'netplan_timed_trial', 'config_object': path, 'automatic_revert_seconds': 240}

    def finalize(self, plan):
        if self.trial:
            self._bus(self.trial, 'Apply')
            self.trial = None
            return {'operation': 'netplan_trial_confirmed'}
        return {'operation': 'no_pending_trial'}

    def observe(self, plan):
        identity = self.identify()
        checks = []
        for check in plan['business_checks']:
            try:
                with socket.create_connection((ip4(check['host']), int(check['port'])), timeout=3):
                    reachable = True
            except OSError:
                reachable = False
            checks.append(dict(check, passed=reachable == check.get('reachable', True)))
        op = next(o for o in plan['operations'] if o['kind'] != 'cloud_init')
        actual = self.read(op)
        target_ok = actual == op['after']
        if op['kind'] == 'netplan':
            live = json.loads(self.output(['ip', '-j', 'address', 'show', 'dev', op['name']]))
            expected = str(ipaddress.ip_interface(plan['params']['address']).ip)
            target_ok &= any(a.get('local') == expected for link in live for a in link.get('addr_info', []))
        return {'at': time.time(), 'identity': identity, 'management_ok': True,
                'guard_ok': bool(checks) and all(c['passed'] for c in checks), 'target_ok': target_ok,
                'checks': checks, 'configuration': actual, 'scope': plan['verification_scope']}
