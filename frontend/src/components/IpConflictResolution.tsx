import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import type { Claimant } from './OwnershipReview'
import { useDisplayTranslation } from './useDisplayTranslation'
import './ip-conflict-resolution.css'

type Ownership = { claimants: Claimant[]; observation_at?: string; observation_stale: boolean; state: { revision: number; expected_mac: string | null } }
async function responseJson(response: Response) {
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    await response.text()
    throw new Error(`HTTP ${response.status}：处置服务暂不可用，请稍后重试`)
  }
  return await response.json()
}
export function IpConflictResolution({ ip, caseId = '', lang = 'zh', onPrepared }: {
  ip: string; caseId?: string; lang?: Lang; onPrepared: (changeId: string) => void
}) {
  const zh = lang === 'zh'
  const [data, setData] = useState<Ownership | null>(null)
  const [mac, setMac] = useState('')
  const [purpose, setPurpose] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [prepared, setPrepared] = useState(false)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const abort = new AbortController()
    fetch('/api/rca/changes/ip-conflict?ip=' + encodeURIComponent(ip), { signal: abort.signal })
      .then(async r => { const value = await responseJson(r); if (!r.ok) throw new Error(value.detail ?? `HTTP ${r.status}`); return value as Ownership })
      .then(setData).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [ip, refresh])
  const display = useDisplayTranslation([...(data?.claimants ?? []).flatMap(c => [c.name ?? '', c.vendor ?? '']), error], lang)
  const prepare = async () => {
    if (!data || !mac) return
    setBusy(true); setError('')
    try {
      const r = await fetch('/api/rca/changes/ip-conflict/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Autopoiesis-Console': 'change-control' }, body: JSON.stringify({ ip, mac, revision: data.state.revision, purpose, case_id: caseId }) })
      const value = await responseJson(r)
      if (!r.ok) throw new Error(value.detail ?? `HTTP ${r.status}`)
      setPrepared(true)
      onPrepared(value.id)
      window.setTimeout(() => document.getElementById('prepared-change-workbench')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  return <section className="ip-resolution" aria-label={zh ? '选择地址归属与处置方案' : 'Address assignment and resolution plan'}>
    <header><h3>{zh ? '选择地址归属' : 'Choose the address owner'} <code>{ip}</code></h3><button onClick={() => { setError(''); setRefresh(n => n + 1) }}>{zh ? '刷新候选' : 'Refresh candidates'}</button></header>
    <p>{zh ? '按实际观测列出争抢设备。选择应保留该地址的设备，读取网关当前配置后生成 DHCP 分配方案，再在下方核对 Diff、批准并执行。' : 'Candidates come from captured observations. Choose the device that should retain this address, read the gateway configuration, then review, approve and execute the DHCP assignment below.'}</p>
    <small>{zh ? '观测时间' : 'Observed at'}: {data?.observation_at ?? (zh ? '等待观测' : 'Awaiting observation')}{data?.observation_stale ? (zh ? ' · 历史快照，执行前重新核验设备' : ' · Historical snapshot; recheck the device before execution') : ''}</small>
    {mac && !data?.claimants.find(c => c.mac === mac)?.in_arp_snapshot ? <p className="ip-resolution-warning">{zh ? '所选设备当前不在 ARP 快照中。可以生成方案供审阅，执行前置检查必须重新确认设备接入口或让终端续租；当前活动 MAC 不一致时流程会停止。' : 'The selected device is absent from the current ARP snapshot. A review plan may be prepared, but preflight must confirm its access port or renew the client lease; a different active MAC stops execution.'}</p> : null}
    <form onSubmit={e => { e.preventDefault(); void prepare() }}>
      <fieldset disabled={busy}><legend>{zh ? '将地址分配给' : 'Assign address to'}</legend>
        {data?.claimants.map(c => <label className="ip-resolution-candidate" key={c.mac}><input type="radio" name={`owner-${ip}`} required checked={mac === c.mac} disabled={c.retired} onChange={() => {
          setMac(c.mac); setPrepared(false)
          const identity = c.name || c.vendor || c.mac
          setPurpose(zh
            ? `将 ${ip} 保留给 ${identity}（${c.mac}）；依据 ${c.samples ?? 0} 次观测、接入接口 ${c.interface || '待核对'}、最近观测 ${c.last_seen || '待核对'}${c.in_arp_snapshot ? '，且仍在当前 ARP 快照中' : '，当前为历史归属记录'}。`
            : `Reserve ${ip} for ${identity} (${c.mac}); evidence: ${c.samples ?? 0} observations, interface ${c.interface || 'pending verification'}, last seen ${c.last_seen || 'pending verification'}${c.in_arp_snapshot ? ', present in the current ARP snapshot' : ', currently a historical ownership record'}.`)
        }} /><span><b>{display.translate(c.name || c.vendor || (zh ? '待核对设备身份' : 'Device identity requires review'))}</b><code>{c.mac}</code><small>{c.interface || ''} · {c.last_seen || ''} · {c.samples ?? 0} {zh ? '次观测' : 'observations'} · {c.in_arp_snapshot ? 'ARP' : zh ? '历史观测' : 'Historical observation'}{c.retired ? (zh ? ' · 已退役' : ' · Retired') : ''}</small></span></label>)}
        {data && !data.claimants.length ? <p>{zh ? '当前观测未包含候选 MAC，请重新采集 ARP。' : 'No candidate MACs in current observations. Capture ARP again.'}</p> : null}
      </fieldset>
      <button disabled={busy || !mac}>{busy ? (zh ? '读取配置并生成方案…' : 'Reading configuration…') : prepared ? (zh ? '重新生成绑定最新配置的方案' : 'Regenerate against current configuration') : zh ? '生成可审批方案与完整命令' : 'Prepare approval plan and commands'}</button>
    </form>
    {error ? <p role="alert">{display.translate(error)}</p> : null}
    {display.error ? <p role="alert">{display.error} <button onClick={display.retry}>Retry translation</button></p> : null}
    <ol className="ip-resolution-progress" aria-label={zh ? '处置流程' : 'Resolution workflow'}>
      <li className={mac ? 'done' : 'active'}><span>01</span><b>{zh ? '选择归属设备' : 'Choose owner'}</b><small>{mac || (zh ? '等待选择' : 'Awaiting selection')}</small></li>
      <li className={prepared ? 'done' : mac ? 'active' : ''}><span>02</span><b>{zh ? '生成完整方案' : 'Prepare full plan'}</b><small>{zh ? '绑定当前配置、Diff 与回滚命令' : 'Bind current config, diff and rollback'}</small></li>
      <li className={prepared ? 'active' : ''}><span>03</span><b>{zh ? '核对并审批' : 'Review and approve'}</b><small>{zh ? '逐项检查目标、指令与影响' : 'Review target, instructions and impact'}</small></li>
      <li><span>04</span><b>{zh ? '确认执行与回读' : 'Execute and read back'}</b><small>{zh ? '显式按钮提交全部步骤' : 'Explicitly submit the full sequence'}</small></li>
    </ol>

  </section>
}
