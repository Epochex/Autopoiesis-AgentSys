from frontend.gateway.app.network_inventory import managed_devices, owned_addresses, interface_traffic


def appliance():
    return managed_devices({
        'identity': {'items': [{'serial': 'FG-TEST', 'model': 'FG100E'}]},
        'interfaces': {'items': [
            {'name': 'wan1', 'ip': '77.236.99.122 255.255.255.248', 'role': 'wan', 'status': 'up'},
            {'name': 'LACP', 'ip': '192.168.16.1 255.255.240.0', 'type': 'aggregate',
             'members': ['port3', 'port4'], 'secondary_ips': ['192.168.40.1 255.255.255.0']},
            {'name': 'port3', 'ip': '0.0.0.0 0.0.0.0'},
        ]},
    })[0]


def test_interface_addresses_belong_to_one_serial_identity():
    device = appliance()
    assert device['id'] == 'fortigate:FG-TEST'
    assert owned_addresses([device]) == {'77.236.99.122', '192.168.16.1', '192.168.40.1'}
    wan = next(i for i in device['interfaces'] if i['name'] == 'wan1')
    assert wan['addresses'][0] == {'ip': '77.236.99.122', 'cidr': '77.236.99.122/29',
                                   'network': '77.236.99.120/29', 'public': True}
    assert not next(i for i in device['interfaces'] if i['name'] == 'port3')['addresses']
    assert managed_devices({'interfaces': {'items': []}}) == []


def test_interface_direction_including_hairpin_and_local_traffic():
    device = appliance()
    wan = next(i for i in device['interfaces'] if i['name'] == 'wan1')
    base = {'srcip': '192.168.1.5', 'dstip': '8.8.8.8', 'records': '1',
            'firstSeenAt': '2026-09-09 00:00:00', 'lastSeenAt': '2026-09-09 00:01:00'}
    queries = []
    def query(sql):
        queries.append(sql)
        return [
            dict(base, srcintf='LACP', dstintf='wan1', subtype='forward', sent=100, received=500),
            dict(base, srcintf='wan1', dstintf='LACP', subtype='forward', sent=200, received=700),
            dict(base, srcintf='unknown0', dstintf='wan1', subtype='local', sent=10, received=20),
            dict(base, srcintf='wan1', dstintf='wan1', subtype='forward', sent=30, received=40),
        ]
    result = interface_traffic(device, wan, 1, query)
    assert result['totals'] == {'rx': 790, 'tx': 880, 'records': 4, 'forwardBytes': 1640, 'localBytes': 30}
    assert 'LIMIT' not in queries[0]
    assert "event_ts >= now() - INTERVAL 1 DAY" in queries[0]


def test_ambiguous_vdom_interfaces_are_not_combined():
    import pytest
    device = appliance()
    wan = next(i for i in device['interfaces'] if i['name'] == 'wan1')
    device['interfaces'].append(dict(wan, vdom='other'))
    with pytest.raises(ValueError, match='VDOM'):
        interface_traffic(device, wan, 1, lambda _: [])
