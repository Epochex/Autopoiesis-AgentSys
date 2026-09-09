import { describe, expect, it } from 'vitest'
import { addressOwner, endpointDevices } from './network-model'
import type { ManagedDevice } from './network-model'

const gateway: ManagedDevice = { id: 'serial:one', name: 'FortiGate', serial: 'one', interfaces: [
  { id: 'wan', name: 'wan1', vdom: 'root', role: 'wan', status: 'up', members: [], addresses: [{ ip: '77.236.99.122', cidr: '77.236.99.122/29', network: '77.236.99.120/29', public: true }] },
  { id: 'lan', name: 'LACP', vdom: 'root', role: 'lan', status: 'up', members: [], addresses: [{ ip: '192.168.16.1', cidr: '192.168.16.1/20', network: '192.168.16.0/20', public: false }] },
] }
describe('device identity and interface ownership', () => {
  it('resolves both public and private interface IPs to the same managed device', () => {
    expect(addressOwner('77.236.99.122', [gateway])?.device.id).toBe('serial:one')
    expect(addressOwner('192.168.16.1', [gateway])?.device.id).toBe('serial:one')
    expect(addressOwner('8.8.8.8', [gateway])).toBeNull()
  })
  it('groups attested MAC addresses, excludes gateway aliases, and keeps unresolved identities separate', () => {
    const rows = ['77.236.99.122', '192.168.16.1', '192.168.16.2', '192.168.16.3', '192.168.16.4', '192.168.16.5'].map((ip, index) => ({ ip, name: 'same hostname', segment: 's', active24h: true, mac: index === 2 || index === 3 ? 'aa:bb:cc:dd:ee:01' : null }))
    const devices = endpointDevices(rows, [gateway])
    expect(devices).toHaveLength(3)
    expect(devices.find(d => d.mac)?.addresses.map(a => a.ip)).toEqual(['192.168.16.2', '192.168.16.3'])
    expect(devices.every(d => !d.addresses.some(a => a.ip === '77.236.99.122'))).toBe(true)
  })
})
