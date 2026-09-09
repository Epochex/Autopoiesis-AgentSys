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

export function ChangeWorkbench({ ip = '', mac = '', caseId = '', selectedChangeId = '', lang = 'zh' }: { ip?: string; mac?: string; caseId?: string; selectedChangeId?: string; lang?: Lang }) {
  const copy = (value: string) => changeCopy(value, lang)
  const [catalog, setCatalog] = useState<{ devices: Device[]; actions: Action[] } | null>(null)
  const [deviceId, setDeviceId] = useState('fortigate')
  const [actionId, setActionId] = useState('dhcp_reserve')
  const [values, setValues] = useState<Record<string, string>>({ ip, mac })
  const [purpose, setPurpose] = useState('')
  const [approvalReason, setApprovalReason] = useState('')
  const [selected, setSelected] = useState<Change | null>(null)
  const [history, setHistory] = useState<Change[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [restore, setRestore] = useState(false)
  const [deviceInfo, setDeviceInfo] = useState<unknown>(null)
  const [panel, setPanel] = useState<'diff' | 'observe'>('diff')
  const [paused, setPaused] = useState(true)
  const [controlReason, setControlReason] = useState('')
  const [manual, setManual] = useState(false)
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
      setSelected(value); setManual(false); setRestore(false); setPanel('diff'); setApprovalReason('')
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
  }, [history, manual, selected])
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
    setSelected(result); setPanel('diff'); setRestore(false); setApprovalReason(''); await loadHistory()
  }
  return <section className="change-workbench">
    {assessment?.decision?.headline ? <p className="change-decision">{translate(assessment.decision.headline)} · {translate(assessment.decision.disposition ?? assessment.decision.action ?? '')}</p> : null}
    {controlledDemo ? <p>{copy("受控演示 · 脚本按一次性授权自动执行，仅操作本次专用测试服务。生产设备写入保持")}{paused ? copy("暂停") : copy("原有启用状态")}{copy("。完成后清理测试服务，保留本次观察回执。")}</p> : <details className="change-control"><summary>{copy("设备写入 ·")}{paused ? copy("已暂停，可继续预览配置") : copy("已启用，每份方案仍需确认")}</summary><label>{copy("调整原因")}<input value={controlReason} onChange={e => setControlReason(e.target.value)} /></label><button disabled={busy || controlReason.trim().length < 3} onClick={() => void run(async () => { const r = await api('/control', 'POST', { paused: !paused, reason: controlReason }); setPaused(r.paused); setControlReason('') })}>{paused ? copy("启用已确认的设备写入") : copy("暂停设备写入")}</button></details>}
    {error ? <p className="change-error" role="alert">{translate(error)}</p> : null}
    {translating ? <p role="status">Translating saved descriptions into English…</p> : null}
    {translationError ? <p role="alert">{translationError} <button onClick={retryTranslation}>Retry translation</button></p> : null}
    <div className="change-layout">
      <aside><button onClick={() => { setManual(true); setSelected(null); setRestore(false); setError('') }}>{copy("＋ 手动新建变更")}</button><button onClick={() => void run(loadHistory)}>{copy("刷新记录")}</button>{history.map(c => <button className={selected?.id === c.id ? 'selected' : ''} key={c.id} onClick={() => void run(async () => { setManual(false); setSelected(await api('/' + c.id)); setRestore(false); setApprovalReason('') })}><b>{translate(c.plan.title)}</b><span>{copy(states[c.status] ?? c.status)}</span><small>{new Date(c.created_at * 1000).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-GB')} · {Object.values(c.target).join(' · ')}</small></button>)}</aside>
      <main>{!selected && !manual ? <div className="change-summary"><p>{lang === 'zh' ? '生成或选择一份变更方案，然后在这里核对配置差异、批准执行并查看回读结果。' : 'Prepare or select a change plan, then review its configuration diff, approve execution and inspect readback results here.'}</p></div> : !selected ? <form className="change-fields" onSubmit={e => { e.preventDefault(); void run(prepare) }}>
        <label>{copy("执行设备")}<select value={deviceId} onChange={e => { const d = catalog?.devices.find(x => x.id === e.target.value); setDeviceId(e.target.value); setActionId(d?.kind === 'linux' ? 'restart_unit' : 'dhcp_reserve'); setDeviceInfo(null) }}>{catalog?.devices.map(d => <option key={d.id} value={d.id}>{translate(d.label)}{!d.configured ? copy("· 未配置连接") : ''}</option>)}</select></label>
        <label>{copy("变更操作")}<select value={actionId} onChange={e => { setActionId(e.target.value); setValues({ ip, mac }) }}>{catalog?.actions.filter(a => a.kind === device?.kind).map(a => <option key={a.id} value={a.id}>{translate(a.category)} · {translate(a.label)}</option>)}</select></label>
        <div className="wide"><button type="button" onClick={() => void run(async () => setDeviceInfo(await api('/devices/' + deviceId)))}>{copy("读取设备身份与配置范围")}</button>{deviceInfo ? <details open><summary>{copy("设备返回的身份与管理范围")}</summary><pre>{text(deviceInfo)}</pre></details> : null}</div>
        {[...(action?.target_fields ?? []), ...(action?.parameter_fields ?? [])].filter(k => k !== 'device').map(k => <label key={k}>{copy(labels[k] || k)}{action?.target_fields.includes(k) ? ' *' : ''}{k === 'decision' || k === 'enabled' ? <select required value={values[k] ?? ''} onChange={e => setValues({ ...values, [k]: e.target.value })}><option value="">{copy("选择")}</option>{(k === 'decision' ? [['deny', copy("拒绝")], ['accept', copy("允许")]] : [['enable', copy("启用")], ['disable', copy("禁用")]]).map(([v, title]) => <option key={v} value={v}>{copy(title)}</option>)}</select> : <input required={action?.target_fields.includes(k)} value={values[k] ?? ''} onChange={e => setValues({ ...values, [k]: e.target.value })} />}</label>)}
        <label className="wide">{copy("变更目的 *")}<textarea required minLength={3} maxLength={1000} value={purpose} onChange={e => setPurpose(e.target.value)} placeholder={copy("说明需要解决的问题、确认过的设备，以及期望结果")} /></label>
        <p className="wide">{action?.reversible ? copy("保存原配置，异常时恢复并复查。") : copy("此操作有瞬时影响，无法直接撤销；异常时停止并升级人工。")} {copy("实际可执行性由设备身份、配置和保护条件共同确认。")}</p>
        <button disabled={busy}>{copy("读取原配置并生成 Diff")}</button>
      </form> : <>
        <div className="change-summary"><span>{copy(states[selected.status] ?? selected.status)}</span><h3>{restore ? copy("恢复原配置预览") : translate(selected.plan.title)}</h3><p className="change-purpose">{translate(selected.reason)}</p><p className="change-target">{catalog?.devices.find(d => d.id === selected.device_id)?.label ?? selected.device_id} <span>→</span> {Object.values(selected.target).join(' · ')}</p><details className="change-audit"><summary>{copy("验证条件、恢复方式与设备身份")}</summary><p>{translate(selected.plan.verification_scope)}</p><p>{(selected.plan.rollback ? translate(selected.plan.rollback) : undefined) || (selected.plan.manual_restore ? copy("恢复本次修改前配置；检测到其他配置变更时升级人工。") : copy("瞬时动作无逆向恢复，异常升级人工。"))}</p><pre>{text(selected.identity)}</pre><small>{copy("变更")}{selected.id} {copy("· 审批摘要")}{selected.digest.slice(0, 16)} {copy("· 预览有效至")}{new Date(selected.expires_at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</small></details></div>
        {selected.plan.playbook ? <details className="change-playbook" open><summary>{lang === 'zh' ? '执行手册与可运行命令' : 'Playbook and runnable commands'}</summary>{Object.entries(selected.plan.playbook).filter(([step, commands]) => step.endsWith('_commands') && Array.isArray(commands)).map(([step, commands]) => <section key={step}><h4>{({precheck_commands: lang === 'zh' ? '执行前检查' : 'Prechecks', apply_commands: lang === 'zh' ? '应用配置' : 'Apply configuration', rollback_commands: lang === 'zh' ? '恢复原配置' : 'Restore configuration', verify_commands: lang === 'zh' ? '回读验证' : 'Verification'} as Record<string,string>)[step] ?? step}</h4><pre>{commands.join('\n')}</pre></section>)}</details> : null}
        <nav className="change-steps" aria-label={copy("变更详情")}><button aria-pressed={panel === 'diff'} onClick={() => setPanel('diff')}><span>01</span> {copy("核对差异")}<b>→</b></button><button aria-pressed={panel === 'observe'} onClick={() => setPanel('observe')}><span>02</span> {copy("执行观察")}<b>→</b></button></nav>
        {panel === 'diff' ? <Diff operations={selected.plan.operations} reverse={restore} lang={lang} /> : <div className="change-observation"><strong>{readbacks.length}<small> / {selected.plan.observation_samples} {copy("次观察")}</small></strong><p>{copy("每")}{selected.plan.observation_interval} {copy("秒检查配置和业务结果。")}{selected.plan.restore_after ? (lang === 'zh' ? `验证后 ${selected.plan.restore_after} 秒恢复临时配置。` : `Restore temporary configuration ${selected.plan.restore_after} seconds after verification.`) : ''}</p><dl className="change-health">{[['management_ok', copy("管理连接")], ['guard_ok', copy("保护性业务检查")], ['target_ok', copy("目标状态")]].map(([key, title]) => <div key={key}><dt>{copy(title)}</dt><dd>{lastHealth?.[key] === true ? copy("通过") : lastHealth?.[key] === false ? copy("未通过") : copy("等待检查")}</dd></div>)}</dl></div>}
        {['awaiting_approval', 'approved'].includes(selected.status) || restore ? <div className="change-approval"><label>{restore ? copy("恢复原因") : copy("审批依据")}<input value={approvalReason} onChange={e => setApprovalReason(e.target.value)} placeholder={copy("确认设备、差异及影响范围后填写")} /></label>
          {selected.status === 'awaiting_approval' ? <><button disabled={busy || approvalReason.trim().length < 3} onClick={() => void run(async () => { setSelected(await api('/' + selected.id + '/approve', 'POST', { digest: selected.digest, reason: approvalReason })); await loadHistory() })}>{copy("批准这份 Diff")}</button><button disabled={busy} onClick={() => void run(async () => setSelected(await api('/' + selected.id + '/preauthorize', 'POST')))}>{copy("匹配已配置的预授权规则")}</button></> : null}
          {selected.status === 'approved' && !restore ? <button className="change-primary" disabled={busy || paused} onClick={() => void run(async () => { await api('/' + selected.id + '/execute', 'POST'); setPanel('observe'); setSelected(await api('/' + selected.id)) })}>{paused ? copy("写入已暂停") : copy("执行已批准的变更 →")}</button> : null}
          {restore ? <button disabled={busy || approvalReason.trim().length < 3} onClick={() => void run(async () => { await api('/' + selected.id + (['executing', 'observing'].includes(selected.status) ? '/abort' : '/restore'), 'POST', { digest: selected.digest, reason: approvalReason }); setRestore(false); setSelected(await api('/' + selected.id)) })}>{copy("确认停止后续动作并恢复")}</button> : <button disabled={busy || approvalReason.trim().length < 3} onClick={() => void run(async () => setSelected(await api('/' + selected.id + '/cancel', 'POST', { digest: selected.digest, reason: approvalReason })))}>{copy("取消变更")}</button>}
        </div> : null}
        {['succeeded', 'executing', 'observing'].includes(selected.status) && selected.plan.manual_restore && !restore ? <button onClick={() => { setRestore(true); setPanel('diff'); setApprovalReason('') }}>{copy("查看恢复 Diff")}</button> : null}
        {panel === 'observe' ? <><h4>{copy("执行与观察记录")}</h4><ol className="change-events">{timeline.map((e, i) => <li key={i}><time>{new Date(e.at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</time><details open={['execution_failed', 'needs_human', 'worker_error'].includes(e.kind)}><summary>{e.kind === 'business_readback' ? (lang === 'zh' ? `连续观察 · ${readbacks.length} 次记录` : `Continuous observation · ${readbacks.length} records`) : copy(eventLabels[e.kind] ?? states[e.kind] ?? e.kind)}</summary>{e.kind === 'business_readback' ? readbacks.map((r, n) => <details key={n}><summary>{copy("第")}{n + 1} {copy("次 ·")}{new Date(r.at * 1000).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB')}</summary><pre>{text(r.payload)}</pre></details>) : <pre>{text(e.payload)}</pre>}</details></li>)}</ol></> : null}
      </>}</main>
    </div>
  </section>
}
