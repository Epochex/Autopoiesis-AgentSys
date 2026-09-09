import { useState } from 'react'
import type { DeviceHistory, DeviceProfile } from '../types'
import { addressOwner, addressScope, formatBytes, formatTime } from './network-model'
import type { ManagedDevice, ManagedInterface } from './network-model'
import { useRead } from './network-read'

interface InterfaceFlow {
  srcip: string; dstip: string; srcintf: string; dstintf: string; subtype: string
  country?: string; services: string[]; records: number; rx: number; tx: number; lastSeenAt: string
}
interface InterfaceTraffic {
  ok: boolean; days: number; records: InterfaceFlow[]; firstSeenAt?: string; lastSeenAt?: string
  totals: { rx: number; tx: number; forwardBytes: number; localBytes: number; records: number }
}
function ReadState({ error, retry }: { error?: string; retry: () => void }) {
  return <div className="nw-empty" role="status">{error ? <>数据暂不可用 · {error} <button onClick={retry}>重新读取</button></> : '正在读取观测数据…'}</div>
}

export function TrafficPanel({ device, intf, zh, onAddress }: { device: ManagedDevice; intf: ManagedInterface; zh: boolean; onAddress: (ip: string) => void }) {
  const [days, setDays] = useState(1)
  const [scope, setScope] = useState('forward')
  const [search, setSearch] = useState('')
  const read = useRead<InterfaceTraffic>(`/api/rca/interface-traffic?${new URLSearchParams({ device_id: device.id, interface_id: intf.id, days: String(days) })}`)
  const rows = read.data?.records.filter(row => (scope === 'all' || (scope === 'forward' ? row.subtype === 'forward' : row.subtype !== 'forward')) && `${row.srcip} ${row.dstip} ${row.country} ${row.services.join(' ')}`.toLowerCase().includes(search.toLowerCase())) ?? []
  return <section className="nw-section">
    <div className="nw-section-head"><h3>{zh ? '接口承载的流量' : 'Interface traffic'}</h3><select aria-label="流量时间范围" value={days} onChange={e => setDays(Number(e.target.value))}><option value={1}>{zh ? '最近 24 小时' : 'Last 24 hours'}</option><option value={7}>{zh ? '最近 7 天' : 'Last 7 days'}</option></select><button onClick={read.retry}>{zh ? '刷新' : 'Refresh'}</button></div>
    {read.data ? read.data.records.length === 0 ? <p className="nw-empty">{zh ? '所选时间范围内未观测到此接口的流量记录。' : 'No traffic records were observed for this interface in the selected window.'}</p> : <>
      <div className="nw-metrics"><div><span>{zh ? '接口接收 RX' : 'Received RX'}</span><strong>{formatBytes(read.data.totals.rx)}</strong><small>{intf.role === 'wan' ? (zh ? '从外网进入此接口' : 'Internet → interface') : (zh ? '从连接网络进入此接口' : 'Network → interface')}</small></div>
        <div><span>{zh ? '接口发送 TX' : 'Sent TX'}</span><strong>{formatBytes(read.data.totals.tx)}</strong><small>{intf.role === 'wan' ? (zh ? '经此接口发往外网' : 'Interface → Internet') : (zh ? '从此接口发往连接网络' : 'Interface → network')}</small></div>
        <div><span>{zh ? '转发业务收发量' : 'Forwarded traffic'}</span><strong>{formatBytes(read.data.totals.forwardBytes)}</strong><small>{zh ? '经过防火墙的业务会话' : 'Sessions through the firewall'}</small></div>
        <div><span>{zh ? '设备自身收发量' : 'Appliance traffic'}</span><strong>{formatBytes(read.data.totals.localBytes)}</strong><small>{zh ? 'DNS、NTP 及访问设备自身等' : 'Local services and appliance access'}</small></div></div>
      <p className="nw-note">{zh ? '统计口径：所选接口的已记录会话字节，含两个方向。各接口会承载同一转发会话，请勿跨接口相加。' : 'Recorded session bytes across this interface in both directions. Do not add interface totals together.'}</p>
      {intf.role === 'wan' && read.data.totals.forwardBytes === 0 && <p className="nw-empty">{zh ? '当前日志没有返回此出口的有效转发流量，办公室上网总量暂不可计算。可切换“设备自身”查看防火墙自身通信。' : 'No forwarded bytes were returned for this WAN. Office Internet totals are unavailable; appliance traffic is available separately.'}</p>}
      <p className="nw-muted">{zh ? '实际观测范围' : 'Observed coverage'} · {formatTime(read.data.firstSeenAt)} → {formatTime(read.data.lastSeenAt)} UTC</p>
      <div className="nw-section-head"><div className="nw-tabs">{[['forward', zh ? '转发业务' : 'Forwarded'], ['local', zh ? '设备自身' : 'Appliance'], ['all', zh ? '全部' : 'All']].map(([value, label]) => <button key={value} aria-pressed={scope === value} onClick={() => setScope(value)}>{label}</button>)}</div><input aria-label="筛选接口访问记录" placeholder={zh ? '筛选地址 / 国家 / 服务' : 'Filter address / country / service'} value={search} onChange={e => setSearch(e.target.value)} /><span>{rows.length} {zh ? '组访问 · 最新在前' : 'relations · newest first'}</span></div>
      <div className="nw-table-scroll"><table className="nw-table"><thead><tr><th>{zh ? '发起地址' : 'Initiator'}</th><th>{zh ? '访问对象' : 'Destination'}</th><th>{zh ? '目的地国家' : 'Country'}</th><th>{zh ? '服务' : 'Service'}</th><th>RX</th><th>TX</th><th>{zh ? '最近观测 UTC' : 'Last seen UTC'}</th></tr></thead><tbody>{rows.map((row, n) => <tr key={`${row.srcip}:${row.dstip}:${row.subtype}:${n}`}><td><button className="nw-link" onClick={() => onAddress(row.srcip)}>{addressOwner(row.srcip, [device]) ? `${device.name} / ${addressOwner(row.srcip, [device])?.intf.name} · ${row.srcip}` : row.srcip}</button></td><td><button className="nw-link" onClick={() => onAddress(row.dstip)}>{addressOwner(row.dstip, [device]) ? `${device.name} / ${addressOwner(row.dstip, [device])?.intf.name} · ${row.dstip}` : row.dstip}</button></td><td>{row.country || '—'}</td><td>{row.services.join(' · ')}</td><td>{formatBytes(row.rx)}</td><td>{formatBytes(row.tx)}</td><td>{formatTime(row.lastSeenAt)}</td></tr>)}</tbody></table></div>
      {!rows.length && <div className="nw-empty">{zh ? '所选时间范围内没有符合条件的访问记录' : 'No matching records in this window'}</div>}
    </> : <ReadState error={read.error} retry={read.retry} />}
  </section>
}

function TargetLabel({ ip, resolved, managed, zh, onAddress }: { ip: string; resolved?: string | null; managed: ManagedDevice[]; zh: boolean; onAddress: (ip: string) => void }) {
  const owner = addressOwner(ip, managed)
  if (owner) return <><button className="nw-link" onClick={() => onAddress(ip)}>{owner.device.name} / {owner.intf.name}</button><small>{ip} · {zh ? '受管接口' : 'Managed interface'}</small></>
  return <><strong>{resolved || ip}</strong>{resolved && <small>{ip}</small>}<small>{addressScope(ip, managed) === 'private' ? (zh ? '私网' : 'Private') : (zh ? '公网' : 'Public')}</small></>
}

export function AddressPortrait({ ip, managed, zh, onAddress, onInvestigate }: { ip: string; managed: ManagedDevice[]; zh: boolean; onAddress: (ip: string) => void; onInvestigate: (ip: string) => void }) {
  const [days, setDays] = useState(7)
  const live = useRead<DeviceProfile>(`/api/rca/device_profile?ip=${encodeURIComponent(ip)}&lang=${zh ? 'zh' : 'en'}`)
  const history = useRead<DeviceHistory>(`/api/rca/device_history?ip=${encodeURIComponent(ip)}&days=${days}`)
  const [view, setView] = useState('live')
  const [search, setSearch] = useState('')
  const [scope, setScope] = useState('all')
  const destinations = live.data?.live?.destinations ?? []
  const historical = history.data?.destinations ?? []
  return <section className="nw-section"><div className="nw-section-head"><h3>{ip} · {zh ? '访问画像' : 'Traffic portrait'}</h3><button onClick={() => { live.retry(); history.retry() }}>{zh ? '刷新' : 'Refresh'}</button><button onClick={() => onInvestigate(ip)}>{zh ? '调查此地址' : 'Investigate address'}</button></div>
    <div className="nw-tabs"><button aria-pressed={view === 'live'} onClick={() => setView('live')}>{zh ? '当前访问对象' : 'Current destinations'}</button><button aria-pressed={view === 'history'} onClick={() => setView('history')}>{zh ? '历史访问' : 'History'}</button><button aria-pressed={view === 'in'} onClick={() => setView('in')}>{zh ? '访问此地址的来源' : 'Inbound sources'}</button></div>
    {view !== 'in' && <div className="nw-section-head"><div className="nw-tabs">{[['all', zh ? '全部对象' : 'All'], ['public', zh ? '公网' : 'Public'], ['private', zh ? '私网' : 'Private'], ['managed', zh ? '受管接口' : 'Managed interfaces']].map(([value, label]) => <button key={value} aria-pressed={scope === value} onClick={() => setScope(value)}>{label}</button>)}</div><input aria-label="筛选访问对象" value={search} onChange={e => setSearch(e.target.value)} placeholder={zh ? 'IP / 域名 / 国家' : 'IP / domain / country'} /></div>}
    {view === 'live' && (live.data ? <>
      <div className="nw-section-head"><p>{zh ? 'FortiView 当前观测' : 'FortiView current observation'} · {destinations.length} {zh ? '个访问对象' : 'destinations'}</p><button onClick={live.retry}>{zh ? '更新当前观测' : 'Update'}</button></div>
      {!live.data.live && <p className="nw-empty">{zh ? '实时数据源未返回当前会话' : 'Live sessions unavailable'}</p>}
      <div className="nw-table-scroll"><table className="nw-table"><thead><tr><th>{scope === 'public' ? (zh ? '全部公网访问对象' : 'All public destinations') : (zh ? '访问对象' : 'Destinations')}</th><th>{zh ? '国家 / 区域' : 'Country'}</th><th>{zh ? '端口' : 'Port'}</th><th>{zh ? '当前速率' : 'Current rate'}</th><th>{zh ? '已记录流量' : 'Recorded bytes'}</th></tr></thead><tbody>{destinations.filter(row => (scope === 'all' || addressScope(row.ip, managed) === scope) && `${row.ip} ${row.resolved} ${row.country}`.toLowerCase().includes(search.toLowerCase())).map((row, n) => <tr key={`${row.ip}:${row.port}:${n}`}><td><TargetLabel ip={row.ip} resolved={row.resolved} managed={managed} zh={zh} onAddress={onAddress} /></td><td>{row.country || '—'}</td><td>{row.port ?? '—'}</td><td>{row.bps == null ? '—' : `${row.bps.toLocaleString()} bps`}</td><td>{formatBytes(row.bytes)}</td></tr>)}</tbody></table></div>
      {!destinations.length && live.data.live && <p className="nw-empty">{zh ? '当前未观测到访问对象' : 'No current destinations'}</p>}
      <p className="nw-note">{zh ? '实时源没有提供各对象的最近访问时间，保留源端顺序；历史访问按最近观测倒序。' : 'The live source supplies no per-destination timestamps; history is ordered newest first.'}</p>
    </> : <ReadState error={live.error} retry={live.retry} />)}
    {view === 'history' && <><div className="nw-section-head"><select aria-label="历史时间范围" value={days} onChange={e => setDays(Number(e.target.value))}><option value={1}>{zh ? '最近 24 小时' : 'Last 24 hours'}</option><option value={7}>{zh ? '最近 7 天' : 'Last 7 days'}</option></select></div>
      {history.data ? <><div className="nw-metrics"><div><span>{zh ? '此地址发出' : 'Sent by this address'}</span><strong>{formatBytes(history.data.up)}</strong></div><div><span>{zh ? '此地址收到' : 'Received by this address'}</span><strong>{formatBytes(history.data.down)}</strong></div><div><span>{zh ? '访问对象' : 'Destinations'}</span><strong>{historical.length}</strong></div></div>
        <p className="nw-muted">{history.data.widened ? (zh ? `所选范围无记录，已扩展到最近 ${history.data.days} 天。` : `Expanded to ${history.data.days} days.`) : ''}{zh ? '实际覆盖' : 'Coverage'} · {formatTime(history.data.first)} → {formatTime(history.data.last)} UTC</p>
        <div className="nw-table-scroll"><table className="nw-table"><thead><tr><th>{scope === 'public' ? (zh ? '全部公网访问对象' : 'All public destinations') : (zh ? '访问对象' : 'Destinations')}</th><th>{zh ? '国家 / 区域' : 'Country'}</th><th>{zh ? '服务' : 'Services'}</th><th>{zh ? '发出' : 'Sent'}</th><th>{zh ? '收到' : 'Received'}</th><th>{zh ? '最近观测 UTC' : 'Last seen UTC'}</th></tr></thead><tbody>{historical.filter(row => (scope === 'all' || addressScope(row.ip, managed) === scope) && `${row.ip} ${row.country} ${row.services.join(' ')}`.toLowerCase().includes(search.toLowerCase())).map(row => <tr key={row.ip}><td><TargetLabel ip={row.ip} managed={managed} zh={zh} onAddress={onAddress} /></td><td>{row.country || '—'}</td><td>{row.services.join(' · ')}</td><td>{formatBytes(row.up)}</td><td>{formatBytes(row.down)}</td><td>{formatTime(row.lastSeenAt)}</td></tr>)}</tbody></table></div>{!historical.length && <p className="nw-empty">{zh ? '该时间范围没有访问记录' : 'No records in this window'}</p>}</> : <ReadState error={history.error} retry={history.retry} />}</>}
    {view === 'in' && (live.data ? <><p className="nw-muted">{zh ? '采样范围内向此地址发起访问的来源，返回包不单独视为入向访问。' : 'Initiators observed in the sample; reply packets are not separate inbound visits.'} {formatTime(live.data.window?.from)} → {formatTime(live.data.window?.to)}</p><div className="nw-table-scroll"><table className="nw-table"><thead><tr><th>{zh ? '访问来源' : 'Source'}</th><th>{zh ? '来源国家 / 区域' : 'Source country'}</th><th>{zh ? '服务' : 'Service'}</th><th>{zh ? '已记录字节' : 'Recorded bytes'}</th><th>{zh ? '最近观测 UTC' : 'Last seen UTC'}</th></tr></thead><tbody>{live.data.inbound.map((row, n) => <tr key={`${row.ip}:${n}`}><td><button className="nw-link" onClick={() => onAddress(row.ip)}>{row.rdns || row.ip}</button></td><td>{row.country || '—'}</td><td>{row.services.join(' · ')}</td><td>{formatBytes(row.bytes)}</td><td>{formatTime(row.lastSeenAt)}</td></tr>)}</tbody></table></div>{!live.data.inbound.length && <p className="nw-empty">{zh ? '采样中没有入向访问记录' : 'No inbound visits in the sample'}</p>}</> : <ReadState error={live.error} retry={live.retry} />)}
  </section>
}
