"""Explicit FortiOS change plans. Discovery is read-only; only apply() mutates.

The fixed gateway credential is reused, never accepted as a target URL from the
browser. No model-generated command, endpoint or arbitrary configuration blob
can enter this adapter.
"""
from __future__ import annotations

import ipaddress
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from core.remediate.change_control import ChangeError, digest
from .live_identity import _creds, _Session


# Each field is a bounded UI input, not a free-form CLI editor.
CATALOG = {
    'dhcp_reserve': ('固定 / 转移 DHCP 地址', ['ip', 'mac'], ['description'], '地址分配', True),
    'dhcp_unreserve': ('删除 DHCP 地址保留', ['ip'], [], '地址分配', True),
    'dhcp_clear_lease': ('清理单个 DHCP 租约', ['ip'], ['expected_mac'], '地址分配', False),
    'arp_clear': ('清理单条 ARP 缓存', ['ip', 'interface'], ['expected_mac'], '地址分配', False),
    'port_disable': ('隔离设备 / 禁用接入口', ['switch', 'port'], [], '交换机接入', True),
    'port_enable': ('解除隔离 / 启用接入口', ['switch', 'port'], [], '交换机接入', True),
    'port_cycle': ('重新启停接入口', ['switch', 'port'], [], '交换机接入', False),
    'poe_cycle': ('端口 PoE 断电重启', ['switch', 'port'], [], '交换机接入', False),
    'port_vlan': ('修改接入口 VLAN', ['switch', 'port'], ['vlan'], '交换机接入', True),
    'firewall_policy': ('创建 / 修改精确防火墙规则', ['policy_id'], ['name', 'src_interface', 'dst_interface', 'src_address', 'dst_address', 'service', 'decision', 'enabled', 'before_policy'], '访问控制', True),
    'firewall_policy_delete': ('撤销指定防火墙规则', ['policy_id'], [], '访问控制', True),
    'temporary_block': ('临时封禁来源', ['policy_id'], ['name', 'src_interface', 'dst_interface', 'src_address', 'dst_address', 'service', 'duration_seconds', 'before_policy'], '访问控制', True),
    'management_sources': ('调整管理员允许来源', ['admin'], ['trusted_networks'], '管理入口', True),
    'host_static_ip': ('修改终端静态地址', ['device', 'interface'], ['address', 'gateway', 'dns'], '主机网络', True),
    'host_persist_network': ('修复主机地址持久化', ['device', 'interface'], ['address', 'gateway', 'dns'], '主机网络', True),
    'restart_unit': ('重启失败服务', ['device', 'unit'], [], '主机服务', False),
    'bounce_interface': ('重新启停故障网卡', ['device', 'interface'], [], '主机网络', False),
}


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k not in ('q_origin_key', 'uuid')}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def scalar(value, name='value'):
    value = str(value or '')
    if not value or len(value) > 100 or any(c in value for c in '\r\n\x00'):
        raise ChangeError(f'invalid_{name}')
    return value


def ip4(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10'):
        raise ChangeError('protected_or_invalid_address')
    return str(ip)


def mac_address(value):
    mac = str(value).lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac) or int(mac[:2], 16) & 1:
        raise ChangeError('invalid_unicast_mac')
    return mac


def segment(value):
    return urllib.parse.quote(scalar(value), safe='')


def reservation_cli(server_id, reservation_id, value):
    """Render the exact reviewed reservation fields, including inverse removal."""
    lines = ['config system dhcp server', f'    edit {int(server_id)}', '        config reserved-address']
    if value is None:
        lines.append(f'            delete {int(reservation_id)}')
    else:
        lines.append(f'            edit {int(reservation_id)}')
        for key in ('type', 'ip', 'mac', 'action', 'description'):
            if value.get(key) is not None:
                lines.append(f'                set {key} {json.dumps(str(value[key]), ensure_ascii=False)}')
            else:
                lines.append(f'                unset {key}')
        lines.append('            next')
    return '\n'.join(lines + ['        end', '    next', 'end'])


class FortiTransport:
    def __init__(self, certificate_sha256):
        creds = _creds()
        if not creds:
            raise ChangeError('router_credentials_unavailable')
        self.base, self.user, self.password = creds
        if not re.fullmatch(r'[0-9a-f]{64}', certificate_sha256 or ''):
            raise ChangeError('device_tls_certificate_pin_required')
        self.session = _Session(self.base)
        class PinnedConnection(http.client.HTTPSConnection):
            def connect(connection):
                super().connect()
                if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() != certificate_sha256:
                    connection.close()
                    raise ChangeError('device_tls_certificate_changed')
        from .live_identity import _CTX
        class PinnedHandler(urllib.request.HTTPSHandler):
            def https_open(handler, request):
                return handler.do_open(PinnedConnection, request, context=_CTX)
        self.session.opener = urllib.request.build_opener(PinnedHandler(), urllib.request.HTTPCookieProcessor(self.session.jar))
        # Never follow a redirect carrying CSRF credentials to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ChangeError('device_redirect_rejected')
        self.session.opener.add_handler(NoRedirect())
        if not self.session.login(self.user, self.password):
            raise ChangeError('router_login_failed')

    def close(self):
        self.session.logout()

    def request(self, method, path, body=None):
        if not path.startswith(('/api/v2/cmdb/', '/api/v2/monitor/')) or any(c in path for c in '\r\n'):
            raise ChangeError('invalid_device_path')
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
            headers={'X-CSRFTOKEN': self.session.csrf, 'Content-Type': 'application/json'})
        try:
            with self.session.opener.open(request, timeout=12) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if method == 'GET' and exc.code == 404:
                return {'results': [], 'missing': True}
            raise ChangeError(f'device_http_{exc.code}') from exc
        if result.get('status') not in ('success', None) or result.get('http_status', 200) >= 400:
            raise ChangeError(f"device_rejected_{result.get('error', 'unknown')}")
        return result

    def cli(self, command):
        """Only adapter-generated single commands. SSH requires an existing trusted key."""
        host = urllib.parse.urlsplit(self.base).hostname
        env = dict(os.environ, SSHPASS=self.password)
        done = subprocess.run(['sshpass', '-e', 'ssh', '-T', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'NumberOfPasswordPrompts=1',
            f'{self.user}@{host}', command], env=env, text=True, capture_output=True, timeout=25)
        if done.returncode or re.search(r'command fail|parse error|unknown action|permission denied', done.stdout, re.I):
            raise ChangeError('device_cli_failed_or_host_key_untrusted')
        return done.stdout


class FortiAdapter:
    def __init__(self, transport=None, policy=None):
        self.policy = policy or {}
        self.transport = transport or FortiTransport(self.policy.get('certificate_sha256'))
        self.vdom = scalar(self.policy.get('vdom', 'root'))

    def close(self):
        if hasattr(self.transport, 'close'):
            self.transport.close()

    def path(self, resource):
        return resource + ('&' if '?' in resource else '?') + urllib.parse.urlencode({'vdom': self.vdom})

    def rows(self, resource):
        data = self.transport.request('GET', self.path(resource))
        result = data.get('results')
        return result if isinstance(result, list) else [result] if result else []

    def identify(self):
        info = self.transport.request('GET', self.path('/api/v2/monitor/system/status'))
        if not info.get('serial') or not info.get('version'):
            raise ChangeError('device_identity_unavailable')
        if self.policy.get('serial') and info['serial'] != self.policy['serial']:
            raise ChangeError('unexpected_device_serial')
        return {'serial': info['serial'], 'version': info['version'], 'vdom': self.vdom}

    def inventory(self):
        return {'identity': self.identify(),
            'dhcp': [{k: r.get(k) for k in ('id', 'interface', 'status', 'default-gateway', 'netmask', 'ip-range')} for r in self.rows('/api/v2/cmdb/system.dhcp/server')],
            'switches': [{'serial': r.get('switch-id'), 'ports': [{k: p.get(k) for k in ('port-name', 'status', 'vlan', 'poe-capable', 'type')} for p in r.get('ports', [])]} for r in self.rows('/api/v2/cmdb/switch-controller/managed-switch')]}

    def read(self, op):
        if op.get('order'):
            return [r['policyid'] for r in self.rows('/api/v2/cmdb/firewall/policy')]
        if op.get('cli'):
            return self._transient_read(op)
        rows = self.rows(op['resource'])
        if not rows:
            return None
        row = clean(rows[0])
        return {k: row.get(k) for k in op['fields']} if op.get('fields') else row

    def operation(self, resource, after, *, fields=None, identifier=None):
        op = {'key': resource, 'resource': resource, 'after': after, 'fields': fields, 'identifier': identifier}
        op['before'] = self.read(op)
        if op['before'] == after:
            raise ChangeError('configuration_already_matches')
        return op

    def _dhcp(self, ip):
        candidates = []
        for row in self.rows('/api/v2/cmdb/system.dhcp/server'):
            if row.get('status') != 'enable':
                continue
            network = ipaddress.ip_network(f"{row['default-gateway']}/{row['netmask']}", strict=False)
            if ipaddress.ip_address(ip) in network:
                candidates.append(row)
        if len(candidates) != 1:
            raise ChangeError('dhcp_server_not_unique_for_address')
        return candidates[0]

    def _port(self, target):
        resource = '/api/v2/cmdb/switch-controller/managed-switch/' + segment(target['switch']) + '/ports/' + segment(target['port'])
        rows = self.rows(resource)
        if len(rows) != 1:
            raise ChangeError('managed_switch_port_not_found')
        port = rows[0]
        approved = self.policy.get('access_ports', [])
        if {'switch': target['switch'], 'port': target['port']} not in approved:
            raise ChangeError('port_requires_verified_access_port_inventory')
        if (port.get('type') == 'trunk' or port.get('stacking-port') or port.get('isl-peer-device-name')
            or port.get('fgt-peer-device-name')):
            raise ChangeError('uplink_or_shared_port_protected')
        return resource, port

    def _references(self, resource, name):
        name = scalar(name)
        if not self.rows(resource + '/' + segment(name)):
            raise ChangeError('referenced_device_object_not_found:' + name)
        return [{'name': name}]

    def prepare(self, action, target, params):
        if action not in CATALOG or action.startswith('host_') or action in ('restart_unit', 'bounce_interface'):
            raise ChangeError('unsupported_action_for_device')
        title, required, allowed, _, reversible = CATALOG[action]
        if set(target) != set(required) or set(params) - set(allowed):
            raise ChangeError('unexpected_or_missing_action_fields')
        plan = {'title': title, 'operations': [], 'observation_interval': 15, 'observation_samples': 12,
                'verification_scope': '设备配置与指定业务探测', 'manual_restore': reversible,
                'business_checks': self.policy.get('business_checks', []), 'action': action,
                'target': target, 'params': params}
        if not plan['business_checks']:
            raise ChangeError('configure_independent_business_checks_before_device_writes')
        if action in ('firewall_policy', 'temporary_block', 'management_sources'):
            rules = self.policy.get('effect_checks', {}).get(action, [])
            rule = next((r for r in rules if r.get('target') == target and r.get('params') == params and r.get('checks')), None)
            if not rule:
                raise ChangeError('exact_target_effect_checks_required_before_write')
            plan['effect_checks'] = rule['checks']
        if action.startswith('dhcp_'):
            ip = ip4(target['ip'])
            server = self._dhcp(ip)
            if ip == server['default-gateway'] or ip in self.policy.get('protected_ips', []):
                raise ChangeError('management_or_gateway_address_protected')
            parent = f"/api/v2/cmdb/system.dhcp/server/{server['id']}/reserved-address"
            reservations = self.rows(parent)
            matches = [r for r in reservations if r.get('ip') == ip]
            if len(matches) > 1:
                raise ChangeError('multiple_reservations_require_manual_resolution')
            if action == 'dhcp_reserve':
                mac = mac_address(target['mac'])
                if not any(ipaddress.ip_address(r['start-ip']) <= ipaddress.ip_address(ip) <= ipaddress.ip_address(r['end-ip']) for r in server.get('ip-range', [])):
                    raise ChangeError('address_outside_dhcp_pool')
                if any(r.get('mac') == mac and r.get('ip') != ip for r in reservations):
                    raise ChangeError('mac_has_another_reservation')
                leases = self.rows('/api/v2/monitor/system/dhcp')
                from domains.network_rca.environment import load_arp_snapshot
                from domains.network_rca.ownership_decisions import read_decisions
                known = {(r.get('ip'), str(r.get('mac', '')).lower()) for r in leases + load_arp_snapshot()}
                known |= {(ip, r['mac']) for r in read_decisions(ip)}
                if (ip, mac) not in known:
                    from domains.network_rca.environment import build_environment_report
                    report = build_environment_report()
                    for finding in report.get('findings', []) + report.get('resolved', []):
                        if finding.get('subject') == ip and finding.get('fault_class') == 'duplicate_ip_static':
                            known |= {(ip, str(m).lower()) for m in finding.get('measured', {}).get('macs', [])}
                if (ip, mac) not in known:
                    raise ChangeError('mac_requires_observed_or_registered_identity')
                rid = matches[0]['id'] if matches else max([r['id'] for r in reservations] + [0]) + 1
                after = {'type': 'mac', 'ip': ip, 'mac': mac, 'action': 'reserved',
                         'description': str(params.get('description', 'Autopoiesis approved assignment'))[:255]}
                fields = list(after) if matches else None
                if not matches:
                    after['id'] = rid
                plan['operations'] = [self.operation(parent + f'/{rid}', after, fields=fields, identifier={'id': rid})]
                # New objects have defaults added by FortiOS. Compare the fields written.
                plan['operations'][0]['fields'] = list(after)
                plan['verification_scope'] = 'DHCP 保留配置；客户端重新获址及冲突消失需另行观察'
                plan['playbook'] = {
                    'scope': 'dhcp_reservation',
                    'precheck_commands': ['show system dhcp server', 'execute dhcp lease-list', 'get system arp'],
                    'apply_commands': [reservation_cli(server['id'], rid, after)],
                    'rollback_commands': [reservation_cli(server['id'], rid, plan['operations'][0]['before'])],
                    'verify_commands': ['execute dhcp lease-list', 'get system arp'],
                    'requires_client_renewal': True,
                    'static_conflict_requires_endpoint_or_port_change': True,
                }
            elif action == 'dhcp_unreserve':
                if not matches:
                    raise ChangeError('reservation_not_found')
                plan['operations'] = [self.operation(parent + f"/{matches[0]['id']}", None, identifier={'id': matches[0]['id']})]
            else:
                plan['operations'] = [self._transient('lease', ip, server['interface'], params)]
        elif action == 'arp_clear':
            ip = ip4(target['ip'])
            if ip in self.policy.get('protected_ips', []):
                raise ChangeError('management_address_protected')
            interface = scalar(target['interface'])
            if not self.rows('/api/v2/cmdb/system/interface/' + segment(interface)):
                raise ChangeError('interface_not_found')
            plan['operations'] = [self._transient('arp', ip, interface, params)]
        elif action.startswith('port_') or action == 'poe_cycle':
            resource, port = self._port(target)
            field = 'poe-status' if action == 'poe_cycle' else 'vlan' if action == 'port_vlan' else 'status'
            if action == 'poe_cycle' and not port.get('poe-capable'):
                raise ChangeError('port_not_poe_capable')
            after = 'down' if action in ('port_disable', 'port_cycle') else 'up' if action == 'port_enable' else 'disable' if action == 'poe_cycle' else scalar(params.get('vlan'))
            if action == 'port_vlan':
                self._references('/api/v2/cmdb/system/interface', after)
            op = self.operation(resource, {field: after}, fields=[field])
            plan['operations'].append(op)
            if action in ('port_cycle', 'poe_cycle'):
                op['settle_seconds'] = 3
                plan['operations'].append(dict(op, before=op['after'], after=op['before'], settle_seconds=5))
        elif action in ('firewall_policy', 'temporary_block', 'firewall_policy_delete'):
            pid = int(target['policy_id'])
            if pid <= 0:
                raise ChangeError('invalid_policy_id')
            resource = f'/api/v2/cmdb/firewall/policy/{pid}'
            existing = self.rows(resource)
            protected = self.policy.get('protected_policy_ids', [])
            if pid in protected:
                raise ChangeError('management_policy_protected')
            if action == 'firewall_policy_delete':
                if not existing:
                    raise ChangeError('policy_not_found')
                plan['operations'] = [self.operation(resource, None, identifier={'policyid': pid})]
            else:
                needed = {'name', 'src_interface', 'dst_interface', 'src_address', 'dst_address', 'service'}
                if not needed <= set(params):
                    raise ChangeError('explicit_source_destination_and_service_required')
                if action == 'temporary_block' and existing:
                    raise ChangeError('temporary_block_requires_new_policy_id')
                after = {'name': scalar(params['name']), 'action': 'deny' if action == 'temporary_block' else params.get('decision'),
                         'status': 'enable' if action == 'temporary_block' else params.get('enabled', 'enable'), 'schedule': 'always', 'logtraffic': 'all'}
                if after['action'] not in ('accept', 'deny') or after['status'] not in ('enable', 'disable'):
                    raise ChangeError('invalid_policy_action_or_status')
                for field, param, table in [('srcintf', 'src_interface', 'system/interface'), ('dstintf', 'dst_interface', 'system/interface'),
                    ('srcaddr', 'src_address', 'firewall/address'), ('dstaddr', 'dst_address', 'firewall/address'), ('service', 'service', 'firewall.service/custom')]:
                    if str(params[param]).lower() in ('all', 'any'):
                        raise ChangeError('broad_policy_scope_requires_separate_review')
                    after[field] = self._references('/api/v2/cmdb/' + table, params[param])
                # Exact existing objects only. New address/service objects are separate changes.
                if not existing:
                    after['policyid'] = pid
                op = self.operation(resource, after, fields=list(after), identifier={'policyid': pid})
                plan['operations'] = [op]
                if not existing:
                    order = [r['policyid'] for r in self.rows('/api/v2/cmdb/firewall/policy')]
                    anchor = int(params.get('before_policy', 0))
                    if anchor not in order:
                        raise ChangeError('new_rule_requires_explicit_existing_insertion_anchor')
                    desired = order[:order.index(anchor)] + [pid] + order[order.index(anchor):]
                    order_op = {'key': 'firewall-policy-order', 'order': True, 'policy_id': pid,
                                'before': order + [pid], 'after': desired, 'deferred': True}
                    plan['preconditions'] = [dict(order_op, before=order)]
                    # Install disabled, move to reviewed position, then activate.
                    enabled = dict(after)
                    op['after'] = dict(after, status='disable')
                    plan['operations'] += [order_op, dict(op, before=op['after'], after=enabled, deferred=True)]
                plan['verification_scope'] = '规则配置与业务探测；规则顺序及命中效果需检查流量证据'
                if action == 'temporary_block':
                    duration = int(params.get('duration_seconds', 300))
                    if not 60 <= duration <= 3600:
                        raise ChangeError('temporary_block_duration_60_to_3600_seconds')
                    plan['restore_after'] = duration
        elif action == 'management_sources':
            if not self.policy.get('out_of_band_verified'):
                raise ChangeError('independent_management_recovery_path_required')
            networks = params.get('trusted_networks')
            if not isinstance(networks, list) or not 1 <= len(networks) <= 10:
                raise ChangeError('one_to_ten_trusted_networks_required')
            networks = [ipaddress.IPv4Network(n, strict=False) for n in networks]
            source = self.policy.get('management_source_ip')
            if not source or not any(ipaddress.ip_address(source) in n for n in networks):
                raise ChangeError('current_management_source_must_remain_allowed')
            # Repeating a configured host is preferable to 0.0.0.0/0 in unused slots.
            networks += [networks[-1]] * (10 - len(networks))
            after = {f'trusthost{i+1}': f'{n.network_address} {n.netmask}' for i, n in enumerate(networks)}
            plan['operations'] = [self.operation('/api/v2/cmdb/system/admin/' + segment(target['admin']), after, fields=list(after))]
        return plan

    def _transient(self, kind, ip, interface, params):
        if not self.policy.get('ssh_host_key_verified'):
            raise ChangeError('device_ssh_host_key_must_be_verified')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,35}', interface):
            raise ChangeError('unsupported_cli_interface_name')
        op = {'key': f'{kind}:{interface}:{ip}', 'cli': kind, 'ip': ip, 'interface': interface,
              'irreversible': True, 'after': None, 'expected_mac': mac_address(params['expected_mac']) if params.get('expected_mac') else None}
        op['before'] = self._transient_read(op)
        if not op['before']:
            raise ChangeError('target_cache_entry_not_found')
        return op

    def _transient_read(self, op):
        if op['cli'] == 'lease':
            return [{k: r.get(k) for k in ('ip', 'mac', 'interface')} for r in self.rows('/api/v2/monitor/system/dhcp') if r.get('ip') == op['ip']]
        output = self.transport.cli('get system arp')
        return [line.strip() for line in output.splitlines() if line.split() and line.split()[0] == op['ip'] and op['interface'] in line.split()]

    def apply(self, op, value):
        if op.get('order'):
            pid = op['policy_id']
            index = value.index(pid)
            where, anchor = ('before', value[index + 1]) if index + 1 < len(value) else ('after', value[index - 1])
            result = self.transport.request('PUT', self.path(f'/api/v2/cmdb/firewall/policy/{pid}?action=move&{where}={anchor}'))
            return {'operation': 'policy_move', 'policy_id': pid, where: anchor, 'status': result.get('status')}
        if op.get('cli'):
            command = f"execute dhcp lease-clear {op['ip']}" if op['cli'] == 'lease' else f"diagnose ip arp delete {op['interface']} {op['ip']}"
            output = self.transport.cli(command)
            return {'command': command, 'reply': output[:500], 'at': time.time()}
        current = self.read(op)
        if value is None:
            method, resource, body = 'DELETE', op['resource'], None
        elif current is None:
            method, resource, body = 'POST', op['resource'].rsplit('/', 1)[0], {**value, **(op.get('identifier') or {})}
        else:
            method, resource, body = 'PUT', op['resource'], value
        result = self.transport.request(method, self.path(resource), body)
        return {'method': method, 'resource': resource, 'status': result.get('status'), 'revision': result.get('revision'), 'at': time.time()}

    def observe(self, plan):
        identity = self.identify()
        checks = []
        for check in plan.get('business_checks', []):
            host = ip4(check['host'])
            port = int(check['port'])
            if not 1 <= port <= 65535:
                raise ChangeError('invalid_probe_port')
            try:
                with socket.create_connection((host, port), timeout=3):
                    reachable = True
            except OSError:
                reachable = False
            checks.append({'host': host, 'port': port, 'reachable': reachable, 'passed': reachable == check.get('reachable', True)})
        # State-changing config is checked separately. Transient clears require a new identity reading.
        target_ok = True
        observations = []
        for op in plan['operations']:
            if op.get('cli'):
                current = self._transient_read(op)
                observations.append({'key': op['key'], 'current': current})
                expected_mac = op.get('expected_mac')
                if expected_mac:
                    target_ok &= bool(current) and all(expected_mac in (json.dumps(r) if isinstance(r, dict) else r).lower() for r in current)
                else:
                    target_ok &= not current
        if plan['action'] == 'dhcp_reserve':
            leases = [r for r in self.rows('/api/v2/monitor/system/dhcp') if r.get('ip') == plan['target']['ip']]
            expected_mac = plan['target']['mac'].lower()
            observations.append({'kind': 'dhcp_client_identity', 'ip': plan['target']['ip'],
                                 'macs': [r.get('mac') for r in leases]})
            target_ok &= bool(leases) and all(str(r.get('mac', '')).lower() == expected_mac for r in leases)
        if plan['action'] in ('firewall_policy', 'temporary_block', 'management_sources'):
            # Configuration and generic reachability alone do not prove an access-policy effect.
            effect_checks = plan.get('effect_checks', [])
            if not effect_checks:
                target_ok = False
            for check in effect_checks:
                try:
                    with socket.create_connection((ip4(check['host']), int(check['port'])), timeout=3):
                        reachable = True
                except OSError:
                    reachable = False
                passed = reachable == check['reachable']
                observations.append(dict(check, passed=passed))
                target_ok &= passed
        return {'at': time.time(), 'management_ok': True, 'identity': identity,
                'guard_ok': bool(checks) and all(c['passed'] for c in checks), 'target_ok': bool(target_ok),
                'checks': checks, 'observations': observations, 'scope': plan['verification_scope']}
