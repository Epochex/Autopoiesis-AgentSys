import type { Lang } from '../i18n'
import { changeCopy } from './change-copy'
import { useDisplayTranslation } from './useDisplayTranslation'
import { useCallback, useEffect, useState } from 'react'
import './change-workbench.css'

type Action = { id: string; label: string; target_fields: string[]; parameter_fields: string[]; category: string; reversible: boolean; kind: string }
type Device = { id: string; label: string; kind: string; configured: boolean }
type Operation = { key: string; before: unknown; after: unknown; irreversible?: boolean }
type Change = { id: string; status: string; digest: string; device_id: string; action: string; reason: string; case_id: string; created_at: number; expires_at: number; identity: Record<string, unknown>; target: Record<string, string>; approval?: { actor: string; reason: string }; plan: { playbook?: {precheck_commands: string[]; apply_commands: string[]; rollback_commands: string[]; verify_commands: string[]}; title: string; operations: Operation[]; verification_scope: string; manual_restore: boolean; rollback?: string; observation_interval: number; observation_samples: number; restore_after?: number }; events?: { at: number; kind: string; payload: unknown }[] }
const labels: Record<string, string> = { ip: 'IP 地址', mac: '指定设备 MAC', description: '设备说明', expected_mac: '重新学习后应出现的 MAC', interface: '设备接口', switch: '交换机序列号', port: '接入口', vlan: '目标 VLAN 名称', policy_id: '策略编号', name: '策略名称', src_interface: '来源接口', dst_interface: '目标接口', src_address: '来源地址对象', dst_address: '目标地址对象', service: '服务对象', decision: '策略动作', enabled: '策略状态', duration_seconds: '有效时长（秒）', admin: '设备管理员', trusted_networks: '允许来源 CIDR（逗号分隔）', device: '主机', unit: '服务单元', address: 'IP / 前缀长度', gateway: '网关', dns: 'DNS 地址（逗号分隔）' }
const states: Record<string, string> = { awaiting_approval: '待审批', approved: '已审批，待执行', executing: '执行中', observing: '持续观察中', succeeded: '变更验证通过', needs_human: '需要人工处理', rolled_back: '已恢复变更前配置', restoring: '正在恢复', restored: '已恢复原配置', expired: '预览已过期', cancelled: '已取消' }
const eventLabels: Record<string, string> = { prepared: '保存变更前配置与方案', approved: '审批通过', executing: '开始执行', operation_started: '提交配置', operation_receipt: '设备回执', observing: '开始观察', business_readback: '业务与管理连接检查', execution_failed: '发现异常', restore_started: '开始恢复', restore_receipt: '恢复命令回执', recovery_health: '恢复后健康检查', succeeded: '验证通过', needs_human: '升级人工', rolled_back: '回退完成', final_commit_receipt: '确认限时配置', restore_approved: '批准恢复原配置', worker_error: '执行未完成' }
const text = (v: unknown) => v === undefined || v === null ? '无' : typeof v === 'string' ? v : JSON.stringify(v, null, 2)
const fieldNames: Record<string, string> = { ...labels, active: '服务状态', state: '接口状态', 'reserved-address': '地址保留', 'mac-address': '设备 MAC', id: '条目编号', type: '分配方式', action: '处理方式', status: '启用状态', 'poe-status': '端口供电', before_policy: '插入到此策略之前', hostname: '设备名称' }
const displayValue = (key: string, value: unknown) => key === 'active' && typeof value === 'string' ? ({ failed: '故障（failed）', active: '运行（active）', inactive: '停止（inactive）' }[value] ?? value) : text(value)

async function api(path = '', method = 'GET', body?: unknown) {
  const response = await fetch('/api/rca/changes' + path, { method, headers: { 'Content-Type': 'application/json', 'X-Autopoiesis-Console': 'change-control' }, ...(body ? { body: JSON.stringify(body) } : {}) })
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    await response.text()
    throw new Error(`HTTP ${response.status}：处置服务暂不可用，请稍后重试`)
  }
  const value = await response.json()
  if (!response.ok) throw new Error(value.detail ?? `HTTP ${response.status}`)
  return value
}

function Diff({ operations, reverse, lang }: { operations: Operation[]; reverse: boolean; lang: Lang }) {
  const copy = (value: string) => changeCopy(value, lang)
  return <div className="change-diff">{operations.map((op, i) => {
    const before = reverse ? op.after : op.before, after = reverse ? op.before : op.after
    const keys = [...new Set([...Object.keys(before && typeof before === 'object' && !Array.isArray(before) ? before : {}), ...Object.keys(after && typeof after === 'object' && !Array.isArray(after) ? after : {})])]
    const rows = keys.length ? keys.filter(k => JSON.stringify((before as Record<string, unknown> | null)?.[k]) !== JSON.stringify((after as Record<string, unknown> | null)?.[k])).map(k => [k, (before as Record<string, unknown> | null)?.[k], (after as Record<string, unknown> | null)?.[k]]) : [[copy("记录"), before, after]]
    return <section key={`${op.key}-${i}`}><h4>{copy("修改")}{String(i + 1).padStart(2, '0')} <small>{rows.length} {copy("项差异")}</small></h4>{op.irreversible ? <p>{copy("瞬时操作无法逆转，执行异常时停止后续动作并升级人工。")}</p> : null}<table><thead><tr><th>{copy("配置项")}</th><th>{copy("当前配置")}</th><th>{copy("执行后 →")}</th></tr></thead><tbody>{rows.map(([key, left, right]) => <tr key={String(key)}><th>{copy(fieldNames[String(key)] ?? String(key))}</th><td><pre>{copy(displayValue(String(key), left))}</pre></td><td><pre>{copy(displayValue(String(key), right))}</pre></td></tr>)}</tbody></table><details className="change-audit"><summary>{copy("配置定位")}</summary><code>{op.key}</code></details></section>
  })}</div>
}

function Playbook({ change, lang }: { change: Change; lang: Lang }) {
  const playbook = change.plan.playbook
  if (!playbook) return null
  const zh = lang === 'zh'
  const phases = [
    { key: 'precheck_commands' as const, number: '01', title: zh ? '执行前检查' : 'Preflight checks', note: zh ? '重新读取 ARP、DHCP 租约和 DHCP 服务配置。任何身份、配置摘要或地址池漂移都会终止执行。' : 'Re-read ARP, DHCP leases and DHCP server configuration. Identity, digest or pool drift stops execution.' },
    { key: 'apply_commands' as const, number: '02', title: zh ? '应用配置' : 'Apply configuration', note: zh ? '设备变更服务按下方 Diff 提交受限 FortiOS API 操作。这里展示逐条对应的 CLI 等价命令，供审批审阅和人工接管。' : 'The device change service submits bounded FortiOS API operations matching the diff. CLI-equivalent commands are shown for approval and manual takeover.' },
    { key: 'verify_commands' as const, number: '03', title: zh ? '配置回读与连续验证' : 'Readback and continuous verification', note: zh ? `提交后回读配置，并每 ${change.plan.observation_interval} 秒检查一次，共 ${change.plan.observation_samples} 次。` : `Read configuration back after submission, then check every ${change.plan.observation_interval} seconds for ${change.plan.observation_samples} samples.` },
    { key: 'rollback_commands' as const, number: 'R', title: zh ? '失败恢复' : 'Failure recovery', note: zh ? '应用失败、回读不一致或保护性业务检查失败时，恢复生成方案时保存的原配置；检测到其他配置变更时停止并升级人工。' : 'Restore the captured before-state after apply, readback or guard-check failure. Concurrent configuration drift stops recovery and escalates to an operator.' },
  ]
  return <section className="change-playbook" aria-label={zh ? '完整执行手册' : 'Complete execution playbook'}>
    <header><div><small>{zh ? '已绑定审批摘要' : 'BOUND APPROVAL DIGEST'} · {change.digest.slice(0, 16)}</small><h3>{zh ? '完整 Playbook 与指令注释' : 'Complete playbook and command notes'}</h3></div><b>{change.plan.operations.length} {zh ? '项受限操作' : 'bounded operations'}</b></header>
    <p>{zh ? '执行按钮提交整套确定性序列：身份复核 → 配置漂移检查 → 应用 → 配置回读 → 连续业务验证。失败路径自动调用恢复步骤并保留每步回执。' : 'The execute button submits one deterministic sequence: identity check → drift check → apply → configuration readback → continuous business verification. Failures invoke recovery and retain step receipts.'}</p>
    <div className="change-playbook-phases">{phases.map(phase => <section key={phase.key} className={phase.key === 'rollback_commands' ? 'rollback' : ''}><span>{phase.number}</span><div><h4>{phase.title}</h4><p>{phase.note}</p><ol>{playbook[phase.key].map((command, index) => <li key={`${phase.key}-${index}`}><small>{zh ? `指令 ${index + 1}，${phase.title}` : `Instruction ${index + 1}, ${phase.title}`}</small><pre>{command}</pre></li>)}</ol></div></section>)}</div>
  </section>
}

export function ChangeWorkbench({ ip = '', mac = '', caseId = '', selectedChangeId = '', lang = 'zh', guided = false }: { ip?: string; mac?: string; caseId?: string; selectedChangeId?: string; lang?: Lang; guided?: boolean }) {
  const copy = (value: string) => changeCopy(value, lang)
  const [catalog, setCatalog] = useState<{ devices: Device[]; actions: Action[] } | null>(null)
  const [deviceId, setDeviceId] = useState('fortigate')
  const [actionId, setActionId] = useState('dhcp_reserve')
  const [values, setValues] = useState<Record<string, string>>({ ip, mac })
  const [purpose, setPurpose] = useState('')
  const [selected, setSelected] = useState<Change | null>(null)
  const [history, setHistory] = useState<Change[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [restore, setRestore] = useState(false)
  const [deviceInfo, setDeviceInfo] = useState<unknown>(null)
  const [panel, setPanel] = useState<'diff' | 'observe'>('diff')
  const [paused, setPaused] = useState(true)
  const [manual, setManual] = useState(false)
  const [reviewConfirmed, setReviewConfirmed] = useState(false)
  const [assessment, setAssessment] = useState<{headline: string; pending_count: number; decision: {headline?: string; disposition?: string; action?: string}} | null>(null)
  const { translate, loading: translating, error: translationError, retry: retryTranslation } = useDisplayTranslation([
    assessment?.headline ?? '', assessment?.decision?.headline ?? '', assessment?.decision?.disposition ?? '', assessment?.decision?.action ?? '',
    ...history.map(change => change.plan.title), ...(catalog?.devices.map(device => device.label) ?? []),
    ...(catalog?.actions.flatMap(action => [action.label, action.category]) ?? []),
    selected?.reason ?? '', selected?.plan.title ?? '', selected?.plan.verification_scope ?? '', selected?.plan.rollback ?? '', error,
  ], lang)
  const action = catalog?.actions.find(a => a.id === actionId)
  const device = catalog?.devices.find(d => d.id === deviceId)
  const readbacks = selected?.events?.filter(e => e.kind === 'business_readback') ?? []
  const controlledDemo = selected?.events?.some(e => e.kind === 'controlled_demo_scope') ?? false
  const lastHealth = readbacks.at(-1)?.payload as Record<string, unknown> | undefined
  const timeline = selected?.events?.filter((e, i, events) => (e.kind !== 'business_readback' || i === events.findIndex(x => x.kind === 'business_readback')) && !(e.kind === 'final_commit_receipt' && (e.payload as Record<string, unknown>)?.operation === 'no_pending_trial')) ?? []
  const run = async (fn: () => Promise<void>) => { setBusy(true); setError(''); try { await fn() } catch (e) { setError(String(e)) } finally { setBusy(false) } }
  const loadHistory = useCallback(async () => { const r = await api(caseId ? `?case_id=${encodeURIComponent(caseId)}` : ''); setHistory(r.changes) }, [caseId])
  useEffect(() => {
    if (!selectedChangeId) return
    let active = true
    void api('/' + encodeURIComponent(selectedChangeId)).then(value => {
      if (!active || (caseId && value.case_id !== caseId)) return
      setSelected(value); setManual(false); setRestore(false); setPanel('diff'); setReviewConfirmed(false)
      void loadHistory()
    }).catch(e => { if (active) setError(String(e)) })
    return () => { active = false }
  }, [selectedChangeId, caseId, loadHistory])
  useEffect(() => { void api('/catalog').then(setCatalog).catch(e => setError(String(e))) }, [])
  useEffect(() => { void loadHistory().catch(e => setError(String(e))) }, [loadHistory])
  useEffect(() => { void api('/control').then(r => setPaused(r.paused)).catch(e => setError(String(e))) }, [])
  useEffect(() => {
    let active = true
    const read = () => { void api('/assessment?case_id=' + encodeURIComponent(caseId)).then(r => { if (active) setAssessment(r) }).catch(e => { if (active) setError(String(e)) }); void loadHistory().catch(() => {}) }
    read(); const timer = window.setInterval(read, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [caseId, loadHistory])
  useEffect(() => {
    if (manual || selected || !history.length) return
    let active = true
    void api('/' + history[0].id).then(p => { if (active) { setSelected(p); setPanel(['succeeded', 'executing', 'observing', 'needs_human', 'rolled_back'].includes(p.status) ? 'observe' : 'diff') } }).catch(e => setError(String(e)))
    return () => { active = false }
  }, [history, manual, selected, guided, selectedChangeId])
  useEffect(() => {
    if (!selected || !['awaiting_approval', 'approved', 'executing', 'observing', 'restoring'].includes(selected.status)) return
    const timer = window.setInterval(() => { void api('/' + selected.id).then(setSelected).catch(e => setError(String(e))) }, 2000)
    return () => window.clearInterval(timer)
  }, [selected?.id, selected?.status]) // eslint-disable-line react-hooks/exhaustive-deps
  const prepare = async () => {
    if (!action) return
    const target = Object.fromEntries(action.target_fields.map(k => [k, k === 'device' ? deviceId : values[k] || '']))
    const params = Object.fromEntries(action.parameter_fields.filter(k => values[k]?.trim()).map(k => [k, ['dns', 'trusted_networks'].includes(k) ? values[k].split(',').map(s => s.trim()).filter(Boolean) : k === 'duration_seconds' ? Number(values[k]) : values[k]]))
    const result = await api('', 'POST', { device_id: deviceId, action: actionId, target, params, purpose, case_id: caseId })
    setSelected(result); setPanel('diff'); setRestore(false); await loadHistory()
  }
  return <section className={`change-workbench${guided ? ' guided' : ''}`} id={guided ? 'prepared-change-workbench' : undefined}>
    {assessment?.decision?.headline ? <p className="change-decision">{translate(assessment.decision.headline)} · {translate(assessment.decision.disposition ?? assessment.decision.action ?? '')}</p> : null}
    {controlledDemo ? <p>{copy("受控演示 · 脚本按一次性授权自动执行，仅操作本次专用测试服务。生产设备写入保持")}{paused ? copy("暂停") : copy("原有启用状态")}{copy("。完成后清理测试服务，保留本次观察回执。")}</p> : <details className="change-control" open={guided && paused && selected?.status === 'approved'}><summary>{copy("设备写入 ·")}{paused ? copy("已暂停，可继续预览配置") : copy("已启用，每份方案仍需确认")}</summary><p>{paused ? copy("启用后仍只执行已经审批且通过前置检查的方案。") : copy("暂停后，待执行方案继续保留，设备写入立即停止。")}</p><button disabled={busy} onClick={() => void run(async () => { const nextPaused = !paused; const r = await api('/control', 'POST', { paused: nextPaused, reason: nextPaused ? 'operator_paused_from_change_workbench' : 'operator_enabled_from_change_workbench' }); setPaused(r.paused) })}>{paused ? copy("启用已确认的设备写入") : copy("暂停设备写入")}</button></details>}
    {error ? <p className="change-error" role="alert">{translate(error)}</p> : null}
    {translating ? <p role="status">Translating saved descriptions into English…</p> : null}
    {translationError ? <p role="alert">{translationError} <button onClick={retryTranslation}>Retry translation</button></p> : null}
    <div className={`change-layout${guided ? ' guided' : ''}`}>
      {!guided ? <aside><button onClick={() => { setManual(true); setSelected(null); setRestore(false); setError('') }}>{copy("＋ 手动新建变更")}</button><button onClick={() => void run(loadHistory)}>{copy("刷新记录")}</button>{history.map(c => <button className={selected?.id === c.id ? 'selected' : ''} key={c.id} onClick={() => void run(async () => { setManual(false); setSelected(await api('/' + c.id)); setRestore(false); setReviewConfirmed(false) })}><b>{translate(c.plan.title)}</b><span>{copy(states[c.status] ?? c.status)}</span><small>{new Date(c.created_at * 1000).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-GB')} · {Object.values(c.target).join(' · ')}</small></button>)}</aside> : null}
      <main>{!selected && !manual ? <div className="change-summary"><p>{lang === 'zh' ? '生成或选择一份变更方案，然后在这里核对配置差异、批准执行并查看回读结果。' : 'Prepare or select a change plan, then review its configuration diff, approve execution and inspect readback results here.'}</p></div> : !selected ? <form className="change-fields" onSubmit={e => { e.preventDefault(); void run(prepare) }}>
        <label>{copy("执行设备")}<select value={deviceId} onChange={e => { const d = catalog?.devices.find(x => x.id === e.target.value); setDeviceId(e.target.value); setActionId(d?.kind === 'linux' ? 'restart_unit' : 'dhcp_reserve'); setDeviceInfo(null) }}>{catalog?.devices.map(d => <option key={d.id} value={d.id}>{translate(d.label)}{!d.configured ? copy("· 未配置连接") : ''}</option>)}</select></label>
        <label>{copy("变更操作")}<select value={actionId} onChange={e => { setActionId(e.target.value); setValues({ ip, mac }) }}>{catalog?.actions.filter(a => a.kind === device?.kind).map(a => <option key={a.id} value={a.id}>{translate(a.category)} · {translate(a.label)}</option>)}</select></label>
        <div className="wide"><button type="button" onClick={() => void run(async () => setDeviceInfo(await api('/devices/' + deviceId)))}>{copy("读取设备身份与配置范围")}</button>{deviceInfo ? <details open><summary>{copy("设备返回的身份与管理范围")}</summary><pre>{text(deviceInfo)}</pre></details> : null}</div>
        {[...(action?.target_fields ?? []), ...(action?.parameter_fields ?? [])].filter(k => k !== 'device').map(k => <label key={k}>{copy(labels[k] || k)}{action?.target_fields.includes(k) ? ' *' : ''}{k === 'decision' || k === 'enabled' ? <select required value={values[k] ?? ''} onChange={e => setValues({ ...values, [k]: e.target.value })}><option value="">{copy("选择")}</option>{(k === 'decision' ? [['deny', copy("拒绝")], ['accept', copy("允许")]] : [['enable', copy("启用")], ['disable', copy("禁用")]]).map(([v, title]) => <option key={v} value={v}>{copy(title)}</option>)}</select> : <input required={action?.target_fields.includes(k)} value={values[k] ?? ''} onChange={e => setValues({ ...values, [k]: e.target.value })} />}</label>)}
        <label className="wide">{copy("变更目的 *")}<textarea required minLength={3} maxLength={1000} value={purpose} onChange={e => setPurpose(e.target.value)} placeholder={copy("说明需要解决的问题、确认过的设备，以及期望结果")} /></label>
        <p className="wide">{action?.reversible ? copy("保存原配置，异常时恢复并复查。") : copy("此操作有瞬时影响，无法直接撤销；异常时停止并升级人工。")} {copy("实际可执行性由设备身份、配置和保护条件共同确认。")}</p>
        <button disabled={busy}>{copy("读取原配置并生成 Diff")}</button>
      </form> : <>
        <div className="change-summary"><span>{copy(states[selected.status] ?? selected.status)}</span><h3>{restore ? copy("恢复原配置预览") : translate(selected.plan.title)}</h3><p className="change-purpose">{translate(selected.reason)}</p><p className="change-target">{translate(catalog?.devices.find(d => d.id === selected.device_id)?.label ?? selected.device_id)} <span>→</span> {Object.values(selected.target).join(' · ')}</p><details className="change-audit"><summary>{copy("验证条件、恢复方式与设备身份")}</summary><p>{translate(selected.plan.verification_scope)}</p><p>{(selected.plan.rollback ? translate(selected.plan.rollback) : undefined) || (selected.plan.manual_restore ? copy("恢复本次修改前配置；检测到其他配置变更时升级人工。") : copy("瞬时动作无逆向恢复，异常升级人工。"))}</p><pre>{text(selected.identity)}</pre><small>{copy("变更")}{selected.id} {copy("· 审批摘要")}{selected.digest.slice(0, 16)} {copy("· 预览有效至")}{new Date(selected.expires_at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</small></details></div>
        <Playbook change={selected} lang={lang} />
        <nav className="change-steps" aria-label={copy("变更详情")}><button aria-pressed={panel === 'diff'} onClick={() => setPanel('diff')}><span>01</span> {copy("核对差异")}<b>→</b></button><button aria-pressed={panel === 'observe'} onClick={() => setPanel('observe')}><span>02</span> {copy("执行观察")}<b>→</b></button></nav>
        {panel === 'diff' ? <Diff operations={selected.plan.operations} reverse={restore} lang={lang} /> : <div className="change-observation"><strong>{readbacks.length}<small> / {selected.plan.observation_samples} {copy("次观察")}</small></strong><p>{copy("每")}{selected.plan.observation_interval} {copy("秒检查配置和业务结果。")}{selected.plan.restore_after ? (lang === 'zh' ? `验证后 ${selected.plan.restore_after} 秒恢复临时配置。` : `Restore temporary configuration ${selected.plan.restore_after} seconds after verification.`) : ''}</p><dl className="change-health">{[['management_ok', copy("管理连接")], ['guard_ok', copy("保护性业务检查")], ['target_ok', copy("目标状态")]].map(([key, title]) => <div key={key}><dt>{copy(title)}</dt><dd>{lastHealth?.[key] === true ? copy("通过") : lastHealth?.[key] === false ? copy("未通过") : copy("等待检查")}</dd></div>)}</dl></div>}
        {['awaiting_approval', 'approved'].includes(selected.status) || restore ? <div className="change-approval">
          {!restore && selected.status === 'awaiting_approval' ? <label className="change-review-confirm"><input type="checkbox" checked={reviewConfirmed} onChange={e => setReviewConfirmed(e.target.checked)} />{lang === 'zh' ? '我已核对目标设备、完整 Playbook、配置 Diff、验证条件和恢复命令' : 'I reviewed the target, complete playbook, configuration diff, verification criteria and recovery commands'}</label> : null}
          {selected.status === 'awaiting_approval' ? <><button disabled={busy || !reviewConfirmed} onClick={() => void run(async () => { setSelected(await api('/' + selected.id + '/approve', 'POST', { digest: selected.digest, reason: 'operator_confirmed_reviewed_playbook' })); await loadHistory() })}>{lang === 'zh' ? '批准完整 Playbook' : 'Approve complete playbook'}</button><button disabled={busy} onClick={() => void run(async () => setSelected(await api('/' + selected.id + '/preauthorize', 'POST')))}>{copy("匹配已配置的预授权规则")}</button></> : null}
          {selected.status === 'approved' && !restore ? <div className="change-execute"><p>{paused ? (lang === 'zh' ? '方案已批准。启用设备写入后，执行按钮开放。' : 'The plan is approved. Enable device writes to unlock execution.') : (lang === 'zh' ? '点击后会提交全部受限操作，并自动进入配置回读、连续验证和失败恢复。' : 'This submits all bounded operations and proceeds through readback, continuous verification and failure recovery.')}</p><button className="change-primary" disabled={busy || paused} onClick={() => void run(async () => { await api('/' + selected.id + '/execute', 'POST'); setPanel('observe'); setSelected(await api('/' + selected.id)) })}>{paused ? (lang === 'zh' ? '等待启用设备写入' : 'Waiting for device writes') : (lang === 'zh' ? '确认并执行完整 Playbook →' : 'Confirm and execute complete playbook →')}</button></div> : null}
          {restore ? <button disabled={busy} onClick={() => void run(async () => { await api('/' + selected.id + (['executing', 'observing'].includes(selected.status) ? '/abort' : '/restore'), 'POST', { digest: selected.digest, reason: 'operator_requested_restore' }); setRestore(false); setSelected(await api('/' + selected.id)) })}>{copy("确认停止后续动作并恢复")}</button> : <button disabled={busy} onClick={() => void run(async () => { setSelected(await api('/' + selected.id + '/cancel', 'POST', { digest: selected.digest, reason: 'operator_cancelled_from_change_workbench' })) })}>{copy("取消变更")}</button>}
        </div> : null}
        {['succeeded', 'executing', 'observing'].includes(selected.status) && selected.plan.manual_restore && !restore ? <button onClick={() => { setRestore(true); setPanel('diff') }}>{copy("查看恢复 Diff")}</button> : null}
        {panel === 'observe' ? <><h4>{copy("执行与观察记录")}</h4><ol className="change-events">{timeline.map((e, i) => <li key={i}><time>{new Date(e.at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</time><details open={['execution_failed', 'needs_human', 'worker_error'].includes(e.kind)}><summary>{e.kind === 'business_readback' ? (lang === 'zh' ? `连续观察 · ${readbacks.length} 次记录` : `Continuous observation · ${readbacks.length} records`) : copy(eventLabels[e.kind] ?? states[e.kind] ?? e.kind)}</summary>{e.kind === 'business_readback' ? readbacks.map((r, n) => <details key={n}><summary>{copy("第")}{n + 1} {copy("次 ·")}{new Date(r.at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</summary><pre>{text(r.payload)}</pre></details>) : <pre>{text(e.payload)}</pre>}</details></li>)}</ol></> : null}
      </>}</main>
    </div>
  </section>
}
