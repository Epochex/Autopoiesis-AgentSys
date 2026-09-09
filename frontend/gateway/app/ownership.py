"""MAC-specific evidence joined to an independently maintained operator decision."""
from domains.network_rca.environment import load_arp_snapshot, arp_snapshot_provenance
from domains.network_rca.ownership_decisions import read_decisions, current_state, assess_assignment


def ownership_view(ip: str, report: dict, identities: dict) -> dict:
    history = read_decisions(ip)
    finding = next((f for f in report.get('findings', []) + report.get('resolved', [])
                    if f['subject'] == ip and f['fault_class'] == 'duplicate_ip_static'), None)
    if not finding and not history:
        raise ValueError('address_has_no_ownership_finding')
    finding = finding or {'measured': {}, 'verification': None}
    measured = finding['measured']
    arp = [r for r in load_arp_snapshot() if r['ip'] == ip]
    provenance = arp_snapshot_provenance()
    observed = sorted({r['mac'].lower() for r in arp})
    state = current_state(history)
    macs = sorted(set(measured.get('macs', [])) | set(measured.get('arp_macs', [])) |
                  set(measured.get('dhcp_leased_macs', [])) | set(observed) | {e['mac'] for e in history})
    if state['expected_mac']:
        macs = sorted(set(macs) | {state['expected_mac']})
    rows = []
    for mac in macs:
        # Never join a conflicted address to a hostname by IP alone.
        identity = identities.get('byMac', {}).get(mac, {})
        stats = next((r for r in measured.get('claimants', []) if r['mac'] == mac), {})
        manual_name = state['device_names'].get(mac)
        rows.append({'mac': mac, 'name': manual_name or identity.get('hostname') or None,
                     'name_source': 'operator' if manual_name else 'device_inventory',
                     'vendor': identity.get('vendor'), 'device_type': identity.get('type'),
                     'os': identity.get('os'), 'identity_fresh': bool(identities.get('fresh')),
                     'first_seen': stats.get('first_seen'), 'last_seen': stats.get('last_seen'),
                     'samples': stats.get('samples'), 'interface': stats.get('interface'),
                     'in_arp_snapshot': mac in observed,
                     'has_historical_dhcp': mac in measured.get('dhcp_leased_macs', []),
                     'retired': mac in state['retired_macs']})
    return {'ip': ip, 'claimants': rows, 'state': state, 'history': history[-20:],
            'observation_at': provenance.get('captured_at'), 'observation_stale': provenance.get('stale', True),
            'assessment': assess_assignment(state, observed, provenance.get('captured_at'), provenance.get('stale', True)),
            'observed_macs': observed, 'device_change_workflow': '/api/rca/changes',
            'device_write_requires_approved_plan': True,
            'verification': finding.get('verification')}
