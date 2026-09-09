import type { Lang } from '../i18n'
import './gateway-interfaces.css'

export interface GatewayDevice {
  id: string; name: string; serial: string
  interfaces: { id: string; name: string; role: string; status: string; kind?: string | null;
    parent?: string | null; members: string[]; vlanId?: number | null;
    addresses: { ip: string; cidr: string; network: string; public: boolean }[] }[]
}

/** Compact identity annotation on the original canvas; does not change graph layout. */
export function GatewayInterfaces({ devices, lang }: { devices: GatewayDevice[]; lang: Lang }) {
  const zh = lang === 'zh'
  if (!devices.length) return null
  return <details className="gateway-interfaces">
    <summary>{zh ? 'FortiGate · 接口地址' : 'FortiGate · Interface addresses'}</summary>
    <div className="gateway-interfaces-body">
      {devices.map(device => <section key={device.id}>
        <strong>{device.name}</strong><p>{device.serial} · {zh ? '以下地址属于同一台设备' : 'These addresses belong to one device'}</p>
        <table><thead><tr><th>{zh ? '接口' : 'Interface'}</th><th>IP / {zh ? '前缀' : 'prefix'}</th><th>{zh ? '用途 / 配置状态' : 'Role / admin state'}</th></tr></thead>
          <tbody>{device.interfaces.filter(intf => intf.addresses.length).map(intf => <tr key={intf.id}>
            <td>{intf.name}</td><td>{intf.addresses.map(address => <div key={address.cidr}>{address.cidr}</div>)}</td>
            <td>{intf.role === 'wan' ? (zh ? '公网出口' : 'WAN') : intf.name === 'AP_MGMT' ? (zh ? 'AP 管理 VLAN' : 'AP management VLAN') : intf.kind === 'aggregate' ? (zh ? '内网聚合口' : 'LAN aggregate') : intf.name === 'mgmt' ? (zh ? '管理口' : 'Management') : intf.role === 'lan' ? (zh ? '内网' : 'LAN') : (zh ? '独立网段' : 'Subnet')}
              {intf.status === 'down' && <div>{zh ? '配置停用' : 'Disabled'} · down</div>}
              {intf.members.length > 0 && <small>{intf.members.join(' + ')}</small>}
              {intf.parent && <small>{zh ? '父接口' : 'Parent'} · {intf.parent}</small>}
            </td>
          </tr>)}</tbody></table>
      </section>)}
    </div>
  </details>
}
