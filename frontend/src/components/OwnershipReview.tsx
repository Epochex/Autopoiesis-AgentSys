import { useEffect, useState, type ReactNode } from 'react'
import type { EnvFinding } from '../types'
import './ownership-review.css'
import { useDisplayTranslation } from './useDisplayTranslation'
import { ChangeWorkbench } from './ChangeWorkbench'

export type Claimant = {
  mac: string; name: string | null; vendor?: string; device_type?: string; os?: string
  name_source?: string
  identity_fresh: boolean; first_seen?: string; last_seen?: string; samples?: number; interface?: string
  in_arp_snapshot: boolean; has_historical_dhcp: boolean; retired: boolean
}
type Ownership = {
  claimants: Claimant[]; observation_at?: string; observation_stale: boolean; assessment: string
  state: { revision: number; expected_mac: string | null }
  history: { revision: number; action: string; mac: string; reason: string; operator_label: string; at: string }[]
}
const states: Record<string, string> = {
  unassigned: '尚未登记人工归属', awaiting_observation: '已登记，等待登记后的新观测',
  matches_latest_observation: '最新 ARP 与登记归属一致，继续监测', conflict_observed: '新观测与登记归属冲突，请重新确认',
}
const actions: Record<string, string> = { set_owner: '登记 / 转移归属', retire_record: '标记旧设备记录失效', clear_owner: '撤销归属登记', label_device: '补充设备名称' }

const englishStates: Record<string, string> = { unassigned: 'No operator assignment recorded', awaiting_observation: 'Assigned; awaiting a new observation', matches_latest_observation: 'Latest ARP matches the assignment; monitoring continues', conflict_observed: 'New observation conflicts with the assignment; review required' }
const englishActions: Record<string, string> = { set_owner: 'Assign or transfer ownership', retire_record: 'Retire a device record', clear_owner: 'Clear ownership assignment', label_device: 'Add a device label' }

export function OwnershipReview({ finding, zh, children }: {
  finding: EnvFinding; zh: boolean; children: (profiles: Claimant[]) => ReactNode
}) {
  const [data, setData] = useState<Ownership | null>(null)
  const [selected, setSelected] = useState('')
  const [action, setAction] = useState('set_owner')
  const [reason, setReason] = useState('')
  const [operator, setOperator] = useState('')
  const [deviceName, setDeviceName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [review, setReview] = useState(false)
  const [receipt, setReceipt] = useState('')
  const [deviceChange, setDeviceChange] = useState(false)
  const url = `/api/rca/environment/ownership?ip=${encodeURIComponent(finding.subject)}`
  useEffect(() => {
    const controller = new AbortController()
    fetch(url, { signal: controller.signal }).then(async r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json() as Ownership)
    }).catch(e => { if (!controller.signal.aborted) setError(String(e)) })
    return () => controller.abort()
  }, [url])
  const refresh = async () => {
    setBusy(true); setError('')
    try {
      const r = await fetch(`${url}&refresh=true`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json() as Ownership)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const submit = async () => {
    if (!data) return
    setBusy(true); setError('')
    try {
      const r = await fetch('/api/rca/environment/ownership/decision', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip: finding.subject, mac: selected, action, revision: data.state.revision, reason, operator, device_name: deviceName }),
      })
      const payload = await r.json()
      if (!r.ok) throw new Error(r.status === 409 ? (zh ? '登记已变化或操作不适用，请刷新后重新确认。' : 'Decision changed; refresh and review.') : String(payload.detail ?? r.status))
      setData(payload.ownership); setEditing(false); setReview(false)
      setReceipt(zh ? '决策已保存。设备网络配置保持原状，采集器继续检测归属变化。' : 'Decision saved. Network configuration unchanged; observation continues.')
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const display = useDisplayTranslation([...(data?.claimants ?? []).flatMap(c => [c.name ?? '', c.vendor ?? '']), ...(data?.history ?? []).map(item => item.reason), error, receipt], zh ? 'zh' : 'en')
  return <section className="ownership-review">
    {display.error && <p role="alert">{display.error} <button onClick={display.retry}>Retry translation</button></p>}
    {children(data?.claimants ?? [])}
    <div className="ownership-status">
      <div><b>{data ? (zh ? states[data.assessment] : englishStates[data.assessment] ?? data.assessment) : (zh ? '正在读取归属登记' : 'Loading assignment')}</b>
        {data?.state.expected_mac ? <span>{zh ? '登记设备' : 'Assigned device'} · {display.translate(data.claimants.find(c => c.mac === data.state.expected_mac)?.name || data.state.expected_mac)}</span> : null}
        <small>{zh ? 'ARP 快照' : 'ARP snapshot'} · {data?.observation_at || (zh ? '等待采集' : 'Awaiting capture')}{data?.observation_stale ? (zh ? ' · 已过期，需重新采集' : ' · Stale; capture again') : ''}</small>
      </div>
      <button disabled={busy} onClick={() => void refresh()}>{busy ? '…' : zh ? '重新核验' : 'Recheck'}</button>
      <button disabled={!data} onClick={() => { setEditing(!editing); setReview(false) }}>{zh ? '人工确认归属' : 'Review assignment'}</button>
      <button disabled={!data} onClick={() => setDeviceChange(!deviceChange)}>{zh ? '设备配置变更' : 'Device change'}</button>
    </div>
    {deviceChange ? <ChangeWorkbench lang={zh ? 'zh' : 'en'} ip={finding.subject} mac={selected || data?.state.expected_mac || ''} /> : null}
    {error ? <p role="alert">{display.translate(error)}</p> : null}
    {receipt ? <p role="status">{display.translate(receipt)}</p> : null}
    {editing && data ? <form className="ownership-form" onSubmit={event => { event.preventDefault(); setReview(true) }}>
      <p>{zh ? '这里保存归属登记。修改 DHCP 或设备配置，请打开“设备配置变更”，查看 Diff 并审批执行。' : 'This form saves inventory assignments. Open Device change to preview and approve network configuration.'}</p>
      <label>{zh ? '设备' : 'Device'}<select required value={selected} onChange={e => { setSelected(e.target.value); setDeviceName(''); setReview(false) }}><option value="">{zh ? '选择设备' : 'Choose device'}</option>{data.claimants.map(c => <option key={c.mac} value={c.mac}>{display.translate(c.name || c.vendor || (zh ? '名称未识别' : 'Unidentified'))} · {c.mac}{c.in_arp_snapshot ? ' · ARP' : ''}</option>)}</select></label>
      <label>{zh ? '操作' : 'Action'}<select value={action} onChange={e => { setAction(e.target.value); setReview(false) }}>{Object.entries(actions).map(([key, label]) => <option key={key} value={key}>{zh ? label : englishActions[key] ?? key}</option>)}</select></label>
      <label>{zh ? '操作人（人工填写）' : 'Operator label'}<input required maxLength={100} value={operator} onChange={e => { setOperator(e.target.value); setReview(false) }} /></label>
      <label>{zh ? '设备名称（人工核对后填写）' : 'Verified device label'}<input required={action === 'label_device'} maxLength={120} value={deviceName} onChange={e => { setDeviceName(e.target.value); setReview(false) }} placeholder={zh ? '可选，例如：会议室打印机' : 'Optional device label'} /></label>
      <label>{zh ? '判断依据' : 'Reason'}<input required maxLength={1000} value={reason} onChange={e => { setReason(e.target.value); setReview(false) }} placeholder={zh ? '例如：已核对设备标签和接入位置' : 'Evidence supporting this decision'} /></label>
      <button type="submit" disabled={busy || !selected || !reason.trim() || !operator.trim()}>{zh ? '检查本次变更' : 'Review change'}</button>
      {review ? <div className="ownership-confirm"><b>{finding.subject} · {zh ? actions[action] : englishActions[action] ?? action}</b><p>{action === 'set_owner' ? `${data.state.expected_mac ?? (zh ? '未登记' : 'Unassigned')} → ${selected}` : action === 'label_device' ? `${selected} · ${deviceName}` : `${selected} · ${zh ? actions[action] : englishActions[action] ?? action}`}</p><p>{zh ? '历史观测保留；旧记录失效后再次被观测到仍会参与冲突检测。' : 'History retained. Reobserved retired devices continue to trigger detection.'}</p><button type="button" disabled={busy} onClick={() => void submit()}>{zh ? '确认保存登记' : 'Confirm assignment'}</button><button type="button" onClick={() => setReview(false)}>{zh ? '返回修改' : 'Back'}</button></div> : null}
    </form> : null}
    {data?.history.length ? <details className="ownership-history"><summary>{zh ? '归属决策记录' : 'Decision history'} · {data.history.length}</summary>{[...data.history].reverse().map(item => <p key={item.revision}><time>{item.at}</time> · {item.operator_label} · {zh ? actions[item.action] : englishActions[item.action] ?? item.action} · <code>{item.mac}</code> · {display.translate(item.reason)}</p>)}</details> : null}
  </section>
}
