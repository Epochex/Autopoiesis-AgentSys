import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import type { Claimant } from './OwnershipReview'
import { useDisplayTranslation } from './useDisplayTranslation'
import './ip-conflict-resolution.css'

type Ownership = { claimants: Claimant[]; observation_at?: string; observation_stale: boolean; state: { revision: number; expected_mac: string | null } }
export function IpConflictResolution({ ip, caseId = '', lang = 'zh', onPrepared }: {
  ip: string; caseId?: string; lang?: Lang; onPrepared: (changeId: string) => void
}) {
  const zh = lang === 'zh'
  const [data, setData] = useState<Ownership | null>(null)
  const [mac, setMac] = useState('')
  const [purpose, setPurpose] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const abort = new AbortController()
    fetch('/api/rca/changes/ip-conflict?ip=' + encodeURIComponent(ip), { signal: abort.signal })
      .then(async r => { const value = await r.json(); if (!r.ok) throw new Error(value.detail ?? `HTTP ${r.status}`); return value as Ownership })
      .then(setData).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [ip, refresh])
  const display = useDisplayTranslation([...(data?.claimants ?? []).flatMap(c => [c.name ?? '', c.vendor ?? '']), error], lang)
  const prepare = async () => {
    if (!data || !mac) return
    setBusy(true); setError('')
    try {
      const r = await fetch('/api/rca/changes/ip-conflict/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Autopoiesis-Console': 'change-control' }, body: JSON.stringify({ ip, mac, revision: data.state.revision, purpose, case_id: caseId }) })
      const value = await r.json()
      if (!r.ok) throw new Error(value.detail ?? `HTTP ${r.status}`)
      onPrepared(value.id)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  return <section className="ip-resolution" aria-label={zh ? '选择地址归属与处置方案' : 'Address assignment and resolution plan'}>
    <header><h3>{zh ? '选择地址归属' : 'Choose the address owner'} <code>{ip}</code></h3><button onClick={() => { setError(''); setRefresh(n => n + 1) }}>{zh ? '刷新候选' : 'Refresh candidates'}</button></header>
    <p>{zh ? '按实际观测列出争抢设备。选择应保留该地址的设备，读取网关当前配置后生成 DHCP 分配方案，再在下方核对 Diff、批准并执行。' : 'Candidates come from captured observations. Choose the device that should retain this address, read the gateway configuration, then review, approve and execute the DHCP assignment below.'}</p>
    <small>{zh ? '观测时间' : 'Observed at'}: {data?.observation_at ?? (zh ? '等待观测' : 'Awaiting observation')}{data?.observation_stale ? (zh ? ' · 历史快照，执行前重新核验设备' : ' · Historical snapshot; recheck the device before execution') : ''}</small>
    <form onSubmit={e => { e.preventDefault(); void prepare() }}>
      <fieldset disabled={busy}><legend>{zh ? '将地址分配给' : 'Assign address to'}</legend>
        {data?.claimants.map(c => <label className="ip-resolution-candidate" key={c.mac}><input type="radio" name={`owner-${ip}`} required checked={mac === c.mac} disabled={c.retired} onChange={() => { setMac(c.mac) }} /><span><b>{display.translate(c.name || c.vendor || (zh ? '待核对设备身份' : 'Device identity requires review'))}</b><code>{c.mac}</code><small>{c.interface || ''} · {c.last_seen || ''} · {c.samples ?? 0} {zh ? '次观测' : 'observations'} · {c.in_arp_snapshot ? 'ARP' : zh ? '历史观测' : 'Historical observation'}{c.retired ? (zh ? ' · 已退役' : ' · Retired') : ''}</small></span></label>)}
        {data && !data.claimants.length ? <p>{zh ? '当前观测未包含候选 MAC，请重新采集 ARP。' : 'No candidate MACs in current observations. Capture ARP again.'}</p> : null}
      </fieldset>
      <label className="ip-resolution-reason">{zh ? '选择依据' : 'Assignment reason'}<input required minLength={3} maxLength={1000} value={purpose} onChange={e => setPurpose(e.target.value)} placeholder={zh ? '填写已核对的设备名称、接入位置与保留地址的原因' : 'Verified device, connection point and reason to retain the address'} /></label>
      <button disabled={busy || !mac || purpose.trim().length < 3}>{busy ? (zh ? '读取配置并生成方案…' : 'Reading configuration…') : zh ? '生成可审批方案与完整命令' : 'Prepare approval plan and commands'}</button>
    </form>
    {error ? <p role="alert">{display.translate(error)}</p> : null}
    {display.error ? <p role="alert">{display.error} <button onClick={display.retry}>Retry translation</button></p> : null}
    <ol className="ip-resolution-steps">
      <li><b>{zh ? '核对设备与分配记录' : 'Verify identity and assignment'}</b><pre>get system arp{'\n'}execute dhcp lease-list{'\n'}show system dhcp server</pre></li>
      <li><b>{zh ? '固定 DHCP 分配' : 'Reserve the DHCP assignment'}</b><p>{zh ? '完整变更与回退命令根据实际 DHCP 服务编号和原配置生成。客户端重新获址后回读租约与管理连接。' : 'Change and rollback commands use the actual DHCP server ID and saved configuration. Read back leases and management connectivity after client renewal.'}</p></li>
      <li><b>{zh ? '处理静态地址争抢' : 'Resolve static address contention'}</b><p>{zh ? 'DHCP 保留控制动态地址分配。静态配置的其他设备需要修改其地址，或在核对设备与独占接入口后隔离端口。下方手动变更支持已登记主机静态地址和受管交换机端口，填写真实接口或交换机与端口后生成独立 Diff。' : 'DHCP reservations control dynamic allocation. Other devices using static addresses require an address change, or isolation after verifying the device and its dedicated access port. Manual changes below support registered host addresses and managed switch ports; enter the verified interface or switch and port to prepare a separate Diff.'}</p></li>
      <li><b>{zh ? '回读确认冲突消失' : 'Confirm contention has cleared'}</b><p>{zh ? '连续检查 ARP、DHCP 和二层归属。DHCP 配置成功后案件继续等待观测，发现其他 MAC 争抢时继续处理。' : 'Check ARP, DHCP and layer-2 ownership repeatedly. A successful DHCP change keeps the case awaiting observations; any competing MAC requires further action.'}</p></li>
    </ol>

  </section>
}
