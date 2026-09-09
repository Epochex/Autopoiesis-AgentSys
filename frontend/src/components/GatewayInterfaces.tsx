import type { Lang } from '../i18n'
import type { Subnet } from '../types'
import './gateway-interfaces.css'

export interface GatewayDevice {
  id: string; name: string; serial: string
  interfaces: { id: string; name: string; role: string; status: string; kind?: string | null;
    parent?: string | null; members: string[]; vlanId?: number | null;
    addresses: { ip: string; cidr: string; network: string; public: boolean }[] }[]
}

/** Dashed links encode configuration ownership, not measured communication. */
export function GatewayInterfaces({ device, selected, subnets, lang, onSelect, onSubnet }: {
  device: GatewayDevice; selected?: string; subnets: Subnet[]; lang: Lang
  onSelect: (id: string) => void; onSubnet: (subnet: Subnet) => void
}) {
  const zh = lang === 'zh'
  const interfaces = device.interfaces.filter(intf => intf.addresses.length)
  return <g className="gateway-relations">
    <text x={840} y={55}>{zh ? '虚线：设备配置归属 · 点击接口高亮，点击网段展开终端' : 'Dashed: configuration ownership · select interface or open subnet'}</text>
    {interfaces.map((intf, index) => {
      const y = interfaces.length === 1 ? 430 : 105 + index * 760 / (interfaces.length - 1)
      const active = selected === intf.id
      const networks = [...new Set(intf.addresses.map(a => a.network))]
      const role = intf.role === 'wan' ? (zh ? '公网出口' : 'WAN') : intf.name === 'AP_MGMT' ? 'AP VLAN' : intf.kind === 'aggregate' ? (zh ? '聚合口' : 'Aggregate') : intf.name === 'mgmt' ? (zh ? '管理口' : 'Management') : (zh ? '内网接口' : 'LAN interface')
      return <g key={intf.id} className={active ? 'gateway-relation selected' : 'gateway-relation'}>
        <path d={`M760 430 C925 430,925 ${y},1030 ${y}`} className="ownership-link" />
        <g role="button" tabIndex={0} aria-label={`${device.name} / ${intf.name}`} onClick={() => onSelect(intf.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(intf.id) } }} className="gateway-interface-node">
          <rect x={1030} y={y - 23} width={240} height={54} />
          <text x={1042} y={y - 7} className="gateway-interface-name">{intf.name} · {role}{intf.status === 'down' ? ' · down' : ''}</text>
          {intf.addresses.map((a, i) => <text key={a.cidr} x={1042} y={y + 9 + i * 14}>{a.cidr}</text>)}
        </g>
        {networks.map((network, i) => {
          const sub = subnets.find(s => s.cidr === network && s.intf === intf.name && s.hosts > 0)
          const ny = y + i * 32
          return <g key={network}>
            <path d={`M1270 ${y} C1310 ${y},1310 ${ny},1350 ${ny}`} className="ownership-link" />
            <g role={sub ? 'button' : undefined} tabIndex={sub ? 0 : undefined} aria-label={sub ? `${zh ? '展开网段' : 'Open subnet'} ${network}` : undefined} onClick={() => { if (sub) onSubnet(sub) }} onKeyDown={e => { if (sub && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSubnet(sub) } }} className={sub ? 'gateway-subnet-node' : ''}>
              <rect x={1345} y={ny - 5} width={10} height={10} />
              <text x={1370} y={ny}>{network}</text>
              <text x={1370} y={ny + 17}>{sub ? `${sub.hosts} ${zh ? '台已知设备 · 展开关系图 ▸' : 'known devices · open ▸'}` : zh ? '配置网段 · 暂无终端观测' : 'Configured subnet · no endpoint observations'}</text>
            </g>
          </g>
        })}
        {active && <text x={1030} y={y + 47}>{intf.members.length ? `${zh ? '成员' : 'Members'}: ${intf.members.join(' + ')}` : intf.parent ? `${zh ? '父接口' : 'Parent'}: ${intf.parent}${intf.vlanId != null ? ` · VLAN ${intf.vlanId}` : ''}` : `${device.name} / ${intf.name}`}</text>}
      </g>
    })}
  </g>
}
