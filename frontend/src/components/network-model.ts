export interface InterfaceAddress { ip: string; cidr: string; network: string; public: boolean }
export interface ManagedInterface {
  id: string; name: string; vdom: string; role: string; status: string
  kind?: string | null; parent?: string | null; members: string[]; vlanId?: number | null
  addresses: InterfaceAddress[]
}
export interface ManagedDevice {
  id: string; serial: string; name: string; model?: string | null
  interfaces: ManagedInterface[]; observedAt?: string | null
}
export interface ObservedAsset {
  ip: string; mac?: string | null; name: string; segment: string; active24h: boolean
  deviceClass?: string | null; risk?: string | null; riskReasons?: string[]; riskCaseIds?: string[]
  activity?: { lastSeenAt?: string; sourceInterface?: string; flows24h: number; bytes24h: number } | null
}
export interface EndpointDevice { id: string; name: string; mac?: string | null; addresses: ObservedAsset[] }
export function endpointDevices(assets: ObservedAsset[], managed: ManagedDevice[]): EndpointDevice[] {
  const owned = new Set(managed.flatMap(d => d.interfaces.flatMap(i => i.addresses.map(a => a.ip))))
  const devices = new Map<string, EndpointDevice>()
  for (const asset of assets) {
    if (owned.has(asset.ip)) continue
    const mac = asset.mac?.toLowerCase()
    const validMac = mac && /^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(mac) && mac !== '00:00:00:00:00:00' && mac !== 'ff:ff:ff:ff:ff:ff'
    const id = validMac ? `mac:${mac}` : `observed-ip:${asset.ip}`
    const device = devices.get(id) ?? { id, name: asset.name, mac: validMac ? mac : null, addresses: [] }
    if (device.name === device.addresses[0]?.ip && asset.name !== asset.ip) device.name = asset.name
    device.addresses.push(asset)
    devices.set(id, device)
  }
  return [...devices.values()].sort((a, b) => {
    const latest = (d: EndpointDevice) => d.addresses.map(a => a.activity?.lastSeenAt ?? '').sort().at(-1) ?? ''
    return latest(b).localeCompare(latest(a)) || a.name.localeCompare(b.name)
  })
}
export function addressOwner(ip: string, devices: ManagedDevice[]) {
  for (const device of devices) for (const intf of device.interfaces) {
    if (intf.addresses.some(a => a.ip === ip)) return { device, intf }
  }
  return null
}
export function addressScope(ip: string, devices: ManagedDevice[]): 'managed' | 'private' | 'public' {
  if (addressOwner(ip, devices)) return 'managed'
  const parts = ip.split('.').map(Number)
  return parts[0] === 10 || parts[0] === 127 || (parts[0] === 192 && parts[1] === 168)
    || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
    || (parts[0] === 169 && parts[1] === 254) || /^(fc|fd|fe80:)/i.test(ip) || ip === '::1' ? 'private' : 'public'
}
export const formatBytes = (value?: number) => {
  if (value == null) return '—'
  const unit = value < 1024 ? 0 : Math.min(4, Math.floor(Math.log(value) / Math.log(1024)))
  return `${(value / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit]}`
}
export const formatTime = (value?: string | null) => value ? value.replace('T', ' ').replace(/(?:\.\d+)?(?:Z|\+00:00)?$/, '') : '未观测'
