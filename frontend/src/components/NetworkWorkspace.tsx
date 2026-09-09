import { useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import { addressOwner, endpointDevices, formatTime } from './network-model'
import type { ManagedDevice, ManagedInterface, ObservedAsset, EndpointDevice } from './network-model'
import { TrafficPanel, AddressPortrait } from './NetworkTrafficViews'
import './network-workspace.css'

interface NetworkData {
  observedAt: string
  inventory: { managedDevices?: ManagedDevice[]; assets: ObservedAsset[] }
}
type Selection = { deviceId: string; interfaceId?: string; ip?: string }

const roleLabel = (intf: ManagedInterface, zh: boolean) => intf.role === 'wan' ? (intf.addresses.length ? (zh ? '公网出口' : 'WAN') : (zh ? 'WAN · 未配置地址' : 'WAN · no address'))
  : intf.kind === 'aggregate' ? (zh ? '内网聚合口' : 'Aggregate')
  : intf.vlanId ? `VLAN ${intf.vlanId}` : intf.name === 'mgmt' ? (zh ? '管理口' : 'Management')
  : intf.role === 'lan' ? (zh ? '内网' : 'LAN') : intf.role === 'dmz' ? 'DMZ' : (zh ? '其他接口' : 'Other')

function InterfaceTable({ device, onSelect, zh }: { device: ManagedDevice; onSelect: (id: string) => void; zh: boolean }) {
  return <div className="nw-table-scroll"><table className="nw-table"><thead><tr>
    <th>{zh ? '接口' : 'Interface'}</th><th>{zh ? 'IP / 前缀' : 'IP / prefix'}</th><th>{zh ? '所属网段' : 'Subnet'}</th>
    <th>{zh ? '用途' : 'Role'}</th><th>{zh ? '配置状态' : 'Admin state'}</th><th>{zh ? '物理 / 逻辑关系' : 'Attachment'}</th>
  </tr></thead><tbody>{device.interfaces.map(intf => <tr key={intf.id}>
    <td><button className="nw-link" onClick={() => onSelect(intf.id)}>{intf.name} <span>↗</span></button></td>
    <td>{intf.addresses.length ? intf.addresses.map(a => <div key={a.cidr}>{a.cidr}</div>) : <span className="nw-muted">{zh ? '未配置 IP' : 'No IP'}</span>}</td>
    <td>{intf.addresses.map(a => <div key={a.cidr}>{a.network}</div>)}</td>
    <td>{roleLabel(intf, zh)}</td><td><span className={`nw-status ${intf.status}`}>{intf.status}</span></td>
    <td>{intf.members.length ? intf.members.join(' + ') : intf.parent || intf.kind || '—'}</td>
  </tr>)}</tbody></table></div>
}

function InterfaceRelations({ device, intf, zh, onSelect }: { device: ManagedDevice; intf: ManagedInterface; zh: boolean; onSelect: (id: string) => void }) {
  const parents = device.interfaces.filter(row => row.id !== intf.id && (row.members.includes(intf.name) || row.name === intf.parent))
  const children = device.interfaces.filter(row => row.id !== intf.id && (intf.members.includes(row.name) || row.parent === intf.name))
  return <div className="nw-owned">{parents.map(row => <button key={row.id} onClick={() => onSelect(row.id)}>{zh ? '所属接口' : 'Parent'} · {row.name} ↗</button>)}{children.map(row => <button key={row.id} onClick={() => onSelect(row.id)}>{zh ? '成员 / 子接口' : 'Member / child'} · {row.name} ↗</button>)}{parents.some(row => row.kind === 'aggregate') && <p className="nw-note">{zh ? '聚合口的会话按逻辑接口记录；物理成员口的数据需接口计数器提供。' : 'Sessions use the aggregate interface name; member-port traffic requires interface counters.'}</p>}</div>
}

function EndpointContext({ endpoint, managed, zh, onSelect }: { endpoint: EndpointDevice; managed: ManagedDevice[]; zh: boolean; onSelect: (selection: Selection) => void }) {
  const attachments = managed.flatMap(device => device.interfaces.filter(intf => endpoint.addresses.some(a =>
    a.activity?.sourceInterface === intf.name || intf.addresses.some(address => address.network === a.segment),
  )).map(intf => ({ device, intf })))
  const reasons = [...new Set(endpoint.addresses.flatMap(a => a.riskCaseIds?.length ? a.riskReasons ?? [] : []))]
  return <><div className="nw-owned"><strong>{zh ? '接入归属' : 'Attached through'}</strong>{attachments.map(({ device, intf }) => <button key={intf.id} onClick={() => onSelect({ deviceId: device.id, interfaceId: intf.id })}>{device.name} / {intf.name} ↗</button>)}{!attachments.length && <span>{zh ? '接入接口待确认' : 'Attachment unconfirmed'}</span>}</div>
    <p className="nw-note">{zh ? '终端地址按已观测 MAC 关联；接入接口依据日志或所属网段定位。' : 'Addresses are associated by observed MAC; attachment follows logs or configured subnet.'}</p>
    {reasons.length > 0 && <section className="nw-section"><h3>{zh ? '需要关注的观测' : 'Observations to review'}</h3><ul>{reasons.map(reason => <li key={reason}>{reason}</li>)}</ul></section>}</>
}

export function NetworkWorkspace({ data, lang, rate, onInvestigate }: { data: NetworkData; lang: Lang; rate: number | null; onInvestigate: (ip: string) => void }) {
  const zh = lang === 'zh'
  const managed = useMemo(() => data.inventory.managedDevices ?? [], [data.inventory.managedDevices])
  const endpoints = useMemo(() => endpointDevices(data.inventory.assets, managed), [data.inventory.assets, managed])
  const [selection, setSelection] = useState<Selection | null>(null)
  const [search, setSearch] = useState('')
  const [showUnaddressed, setShowUnaddressed] = useState(false)
  const selectedId = selection?.deviceId ?? managed[0]?.id ?? endpoints[0]?.id
  const device = managed.find(d => d.id === selectedId)
  const endpoint = endpoints.find(d => d.id === selectedId)
  const intf = device?.interfaces.find(i => i.id === selection?.interfaceId)
  const pickAddress = (ip: string) => {
    const owner = addressOwner(ip, managed)
    if (owner) { setSelection({ deviceId: owner.device.id, interfaceId: owner.intf.id }); return }
    const host = endpoints.find(d => d.addresses.some(a => a.ip === ip))
    setSelection({ deviceId: host?.id ?? `address:${ip}`, ip })
  }
  const matchingEndpoints = endpoints.filter(d => `${d.name} ${d.mac} ${d.addresses.map(a => `${a.ip} ${a.segment}`).join(' ')}`.toLowerCase().includes(search.toLowerCase()))
  const matchingManaged = managed.filter(d => `${d.name} ${d.serial} ${d.interfaces.map(i => `${i.name} ${i.addresses.map(a => `${a.ip} ${a.network}`).join(' ')}`).join(' ')}`.toLowerCase().includes(search.toLowerCase()))
  const attached = (host: EndpointDevice) => intf && host.addresses.some(a => a.activity?.sourceInterface === intf.name || intf.addresses.some(address => address.network === a.segment))
  const attachedHosts = endpoints.filter(attached)
  const ip = selection?.ip ?? endpoint?.addresses[0]?.ip
  return <div className="nw-workspace">
    <header className="nw-header" role="group" aria-label={zh ? '设备目录概览' : 'Device inventory summary'}><div><span className="nw-eyebrow">{zh ? '网络态势 / 设备目录' : 'NETWORK / DEVICE INVENTORY'}</span><h2>{zh ? '从设备看清每个接口' : 'Devices and their interfaces'}</h2></div><div className="nw-header-meta"><span>{managed.length} {zh ? '受管设备' : 'managed'} · {endpoints.length} {zh ? '终端身份' : 'endpoint identities'}</span><small>{zh ? '目录观测' : 'Inventory observed'} {formatTime(data.observedAt)} UTC</small>{rate != null && <small>{rate}/s · {zh ? '日志接收速率' : 'Log ingestion rate'}</small>}</div></header>
    <div className="nw-layout"><aside className="nw-directory"><label className="nw-search"><span>{zh ? '查找设备、接口或地址' : 'Find device, interface or address'}</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="IP / MAC / 名称 / 接口 / 网段" /></label>
      <div className="nw-nav-heading">{zh ? '受管网络设备' : 'Managed network devices'}</div>
      {matchingManaged.map(d => <div key={d.id}><button className="nw-device" aria-current={selectedId === d.id ? 'true' : undefined} onClick={() => setSelection({ deviceId: d.id })}><strong>▣ {d.name}</strong><small>{d.interfaces.length} {zh ? '接口' : 'interfaces'} · {d.serial}</small></button>{selectedId === d.id && <div className="nw-interface-nav">{d.interfaces.filter(i => i.addresses.length || showUnaddressed).map(i => <button key={i.id} aria-current={intf?.id === i.id ? 'true' : undefined} onClick={() => setSelection({ deviceId: d.id, interfaceId: i.id })}><span>{i.name} <small>{i.status}</small></span>{i.addresses.map(a => <small key={a.cidr}>{a.cidr}</small>)}</button>)}<button className="nw-muted" onClick={() => setShowUnaddressed(v => !v)}>{showUnaddressed ? (zh ? '收起无地址接口' : 'Hide unaddressed') : (zh ? '展开无地址接口' : 'Show unaddressed')}</button></div>}</div>)}
      {!managed.length && <p className="nw-note">{zh ? '设备身份待读取；刷新目录后显示接口层级。' : 'Device identity is awaiting refresh.'}</p>}
      <div className="nw-nav-heading">{zh ? '终端' : 'Endpoints'} <span>{matchingEndpoints.length}</span></div><div className="nw-endpoint-nav">{matchingEndpoints.map(d => <button key={d.id} className="nw-device" aria-current={selectedId === d.id ? 'true' : undefined} onClick={() => setSelection({ deviceId: d.id })}><strong>{d.name}</strong><small>{d.addresses.map(a => a.ip).join(' · ')}</small></button>)}</div>
      {!matchingManaged.length && !matchingEndpoints.length && <p className="nw-empty">{zh ? '没有匹配的设备或接口' : 'No matching devices or interfaces'}</p>}
    </aside><main className="nw-content">
      <nav className="nw-breadcrumb" aria-label="设备接口路径"><button onClick={() => setSelection(null)}>{zh ? '设备目录' : 'Devices'}</button><span> / </span><button onClick={() => setSelection({ deviceId: selectedId ?? '' })}>{device?.name ?? endpoint?.name ?? ip}</button>{intf && <><span> / </span><strong>{intf.name}</strong></>}</nav>
      {device && <><div className="nw-title"><div><span className="nw-eyebrow">{intf ? roleLabel(intf, zh) : (zh ? '受管设备' : 'MANAGED DEVICE')}</span><h2>{intf ? `${device.name} / ${intf.name}` : device.name}</h2><p>{intf ? intf.addresses.map(a => a.cidr).join(' · ') || (zh ? '此接口未配置 IP' : 'No IP configured') : `${device.model ?? ''} · ${device.serial}`}</p></div>{intf && <span className={`nw-status ${intf.status}`}>{intf.status}</span>}</div>
        {!intf ? <><div className="nw-device-map"><div className="nw-device-core">▣ {device.name}<small>{device.serial}</small></div><div className="nw-device-ports">{device.interfaces.filter(i => i.addresses.length).map(i => <button key={i.id} onClick={() => setSelection({ deviceId: device.id, interfaceId: i.id })}><strong>{i.name}</strong><span>{roleLabel(i, zh)}</span>{i.addresses.map(a => <small key={a.cidr}>{a.cidr}</small>)}</button>)}</div></div><section className="nw-section"><div className="nw-section-head"><h3>{zh ? '全部接口与地址' : 'All interfaces and addresses'}</h3><span>{device.interfaces.length}</span></div><InterfaceTable device={device} zh={zh} onSelect={interfaceId => setSelection({ deviceId: device.id, interfaceId })} /></section></>
        : <><div className="nw-interface-facts"><span>{zh ? '网段' : 'Subnet'}: {[...new Set(intf.addresses.map(a => a.network))].join(' · ') || '—'}</span><span>VDOM: {intf.vdom}</span>{intf.parent && <span>{zh ? '父接口' : 'Parent'}: {intf.parent}</span>}{intf.members.length > 0 && <span>{zh ? '成员接口' : 'Members'}: {intf.members.join(' + ')}</span>}</div><InterfaceRelations device={device} intf={intf} zh={zh} onSelect={interfaceId => setSelection({ deviceId: device.id, interfaceId })} /><TrafficPanel key={intf.id} device={device} intf={intf} zh={zh} onAddress={pickAddress} />
          {intf.role !== 'wan' && <section className="nw-section"><div className="nw-section-head"><h3>{zh ? '此接口下的终端' : 'Attached endpoints'}</h3><span>{attachedHosts.length}</span></div><p className="nw-muted">{zh ? '依据日志接入接口或地址所属网段关联。' : 'Associated by observed ingress interface or configured subnet.'}</p><div className="nw-host-grid">{attachedHosts.map(host => <button key={host.id} onClick={() => setSelection({ deviceId: host.id })}><strong>{host.name}</strong><small>{host.addresses.map(a => a.ip).join(' · ')}</small></button>)}</div>{!attachedHosts.length && <p className="nw-empty">{zh ? '尚未关联到终端' : 'No associated endpoints'}</p>}</section>}</>}
      </>}
      {!device && ip && <><div className="nw-title"><div><span className="nw-eyebrow">{endpoint ? (zh ? '终端设备' : 'ENDPOINT') : (zh ? '地址观测' : 'ADDRESS OBSERVATION')}</span><h2>{endpoint?.name ?? ip}</h2><p>{endpoint?.mac ? `MAC ${endpoint.mac}` : (zh ? '仅有地址观测，设备身份待确认' : 'Observed address; device identity unconfirmed')}</p></div></div>{endpoint && <div className="nw-addresses">{endpoint.addresses.map(a => <button key={a.ip} aria-pressed={ip === a.ip} onClick={() => setSelection({ deviceId: endpoint.id, ip: a.ip })}><strong>{a.ip}</strong><small>{a.segment}</small><small>{a.active24h ? (zh ? '近24小时有活动' : 'Active in last 24h') : (zh ? '近24小时未见活动' : 'No activity in last 24h')}</small></button>)}</div>}{endpoint && <EndpointContext endpoint={endpoint} managed={managed} zh={zh} onSelect={setSelection} />}<AddressPortrait key={ip} ip={ip} managed={managed} zh={zh} onAddress={pickAddress} onInvestigate={onInvestigate} /></>}
      {!device && !ip && <p className="nw-empty">{zh ? '从左侧选择设备或接口查看观测' : 'Select a device or interface'}</p>}
    </main></div>
  </div>
}
