import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import { useInvestigationCase } from './investigation-case'
import { caseNetworkContext, caseSessionIds, isSessionForCase, objectValue, parsedOutput, type CaseSession } from './case-investigation-data'
import { humanizeIncidentSummary } from './incident-copy'
import { useDisplayTranslation } from './useDisplayTranslation'
import './case-investigation.css'

const display = (value: unknown): string => value == null || value === '' ? 'N/A' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const list = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(objectValue) : []
const evidenceProse = (output: string): string => {
  const structured = parsedOutput(output)
  const description = structured.summary ?? structured.note ?? structured.reason ?? structured.message
  if (typeof description === 'string') return description
  return !output.trim().startsWith('{') && !output.trim().startsWith('[') ? output.slice(0, 1500) : ''
}
const statusLabels: Record<string, [string, string]> = {
  open: ['等待调查', 'Awaiting investigation'], investigating: ['调查中', 'Investigating'],
  escalated: ['等待人工处置', 'Awaiting operator action'], resolved: ['已完成验证', 'Verified resolution'],
  action_ready: ['待审批执行', 'Ready for approval'], observing: ['结果观察中', 'Observing results'],
  waiting: ['等待补齐证据', 'Awaiting evidence'],
}

export function CaseInvestigationPage({ lang, caseId, subject, onOpenSources, onOpenMemory, onOpenActions }: {
  lang: Lang; caseId?: string; subject?: string
  onOpenSources?: () => void; onOpenMemory?: () => void; onOpenActions?: () => void
}) {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const { record, error, refresh } = useInvestigationCase(caseId)
  const [choice, setChoice] = useState<{ caseId: string; sessionId: string } | null>(null)
  const [loaded, setLoaded] = useState<CaseSession | null>(null)
  const [sessionError, setSessionError] = useState<{ id: string; message: string } | null>(null)
  const [retry, setRetry] = useState(0)
  const sessionIds = record ? caseSessionIds(record) : []
  const sessionId = choice && choice.caseId === caseId && sessionIds.includes(choice.sessionId) ? choice.sessionId : sessionIds[0]
  const session = loaded && caseId && sessionId && isSessionForCase(loaded, caseId, sessionId) ? loaded : null
  useEffect(() => {
    if (!sessionId || !caseId) return
    const abort = new AbortController()
    let timer: number | undefined
    const read = async () => {
      try {
        const response = await fetch(`/api/rca/investigate/${encodeURIComponent(sessionId)}`, { signal: abort.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const next = await response.json() as CaseSession
        if (!isSessionForCase(next, caseId, sessionId)) throw new Error('Case/session identity mismatch')
        if (!abort.signal.aborted) { setLoaded(next); setSessionError(null) }
      } catch (caught) {
        if (!abort.signal.aborted) setSessionError({ id: sessionId, message: String(caught) })
      } finally {
        if (!abort.signal.aborted) timer = window.setTimeout(read, 8000)
      }
    }
    void read()
    return () => { abort.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [caseId, sessionId, retry])

  const decision = record?.businessDecision
  const facts = objectValue(record?.sourcePayload?.incidentFacts)
  const { evidence: networkEvidence, context: network } = caseNetworkContext(session)
  const identity = objectValue(network.managedDeviceIdentity)
  const workingSet = session?.working_set ?? {}
  const hypotheses = ['confirmed_hypotheses', 'open_hypotheses', 'rejected_hypotheses'] as const
  const evidence = session?.evidence ?? []
  const decisiveIds = new Set(decision?.evidence.map(item => item.evidenceId) ?? [])
  const state = decision?.state ?? record?.status ?? ''
  const factsSummary = record ? humanizeIncidentSummary(record.summary, facts, zh, record.subject) : ''
  const translation = useDisplayTranslation([factsSummary, decision?.headline ?? '', decision?.summary ?? '', decision?.disposition ?? '', decision?.nextProbe ?? '', ...(decision?.missingObservations.map(item => item.question) ?? []), ...hypotheses.flatMap(key => list(workingSet[key]).map(item => display(item.statement))), ...(decision?.evidence.flatMap(item => [item.label, evidenceProse(item.value)]) ?? []), ...evidence.map(item => evidenceProse(item.output))], lang)
  const prose = translation.translate
  return <main className="case-investigation">
    <header className="ci-header"><div><small>{t('案件调查 · 当前证据与历史过程', 'CASE INVESTIGATION · EVIDENCE AND HISTORY')}</small><h1>{record?.subject ?? subject ?? t('选择案件', 'Select a case')}</h1><code>{caseId ?? t('从待处理事件进入一项调查', 'Open an investigation from the incident list')}</code></div><button onClick={() => { refresh(); setRetry(value => value + 1) }}>{t('刷新', 'Refresh')}</button></header>
    {!caseId ? <p>{t('选择具体案件后显示其事实、证据、调查状态和相关资料。', 'Select a case to view its facts, evidence, investigation state, and sources.')}</p> : null}
    {error ? <p role="alert">{t('案件读取失败', 'Case unavailable')}: {error}</p> : null}
    {caseId && !record && !error ? <p role="status">{t('读取案件记录…', 'Loading case record…')}</p> : null}
    {translation.loading ? <p role="status">Translating investigation narrative…</p> : null}
    {translation.error ? <p role="alert">{translation.error} <button onClick={translation.retry}>Retry translation</button></p> : null}
    {record ? <>
      <section className="ci-decision"><small>{statusLabels[state]?.[zh ? 0 : 1] ?? state}</small><h2>{prose(decision?.headline || factsSummary)}</h2><p>{prose(decision?.summary || factsSummary)}</p><p><b>{t('处置建议', 'Recommended action')}: </b>{decision?.disposition ? prose(decision.disposition) : t('等待调查形成处置决定。', 'Awaiting an investigation decision.')}</p><div className="ci-links">{onOpenSources ? <button onClick={onOpenSources}>{t('调查依据', 'Sources used')} →</button> : null}{onOpenMemory ? <button onClick={onOpenMemory}>{t('案件相关记忆', 'Case memories')} →</button> : null}{onOpenActions ? <button onClick={onOpenActions}>{t('处置与审批', 'Actions and approval')} →</button> : null}</div></section>
      <div className="ci-grid">
        <section><h2>{t('事件事实', 'Incident facts')}</h2><p>{prose(factsSummary)}</p><dl><dt>{t('观测时间', 'Observation window')}</dt><dd>{record.firstSeenAt} → {record.lastSeenAt}</dd><dt>{t('检测规则', 'Detection rule')}</dt><dd>{record.ruleId}</dd><dt>{t('受影响资产', 'Affected assets')}</dt><dd>{(session?.asset_ids ?? decision?.impactedAssets ?? []).join(', ') || record.subject}</dd><dt>{t('管理服务', 'Management service')}</dt><dd>{record.service || 'N/A'}</dd>{['failedLogins', 'distinctSources', 'lockouts', 'windowSeconds', 'username', 'authMethod'].filter(key => facts[key] != null).map(key => <div className="ci-fact" key={key}><dt>{({ failedLogins: t('失败登录', 'Failed logins'), distinctSources: t('来源地址数', 'Source addresses'), lockouts: t('锁定次数', 'Lockouts'), windowSeconds: t('统计窗口（秒）', 'Window (seconds)'), username: t('目标账户', 'Target account'), authMethod: t('认证方式', 'Authentication method') })[key]}</dt><dd>{display(facts[key])}</dd></div>)}</dl>{Array.isArray(facts.topSources) && facts.topSources.length ? <><h3>{t('观测到的来源地址', 'Observed source addresses')}</h3><table><thead><tr><th>{t('来源地址', 'Source address')}</th><th>{t('次数', 'Count')}</th></tr></thead><tbody>{facts.topSources.filter(Array.isArray).map((row, index) => <tr key={index}><td><code>{display(row[0])}</code></td><td>{display(row[1])}</td></tr>)}</tbody></table></> : null}<details><summary>{t('源事实与引用', 'Source facts and references')}</summary><pre>{JSON.stringify(record.sourcePayload, null, 2)}</pre></details></section>
        <section><h2>{t('调查对象与网络背景快照', 'Investigation scope and network snapshot')}</h2><p>{t('此处展示调查当时保存的设备背景。常态网络页面持续展示全网观测。', 'This snapshot records device context collected during the investigation. The network page tracks ongoing observations across the network.')}</p><dl><dt>{t('事件对象', 'Event identity')}</dt><dd>{record.subject}</dd><dt>{t('映射到的调查资产', 'Resolved investigation asset')}</dt><dd>{session?.subject ?? t('等待案件会话记录', 'Awaiting case session')}</dd><dt>{t('设备身份', 'Device identity')}</dt><dd>{[identity.vendor, identity.product, identity.model, identity.serial].filter(Boolean).join(' · ') || t('本案尚未保存设备身份', 'Device identity has not been recorded for this case')}</dd><dt>{t('设备版本', 'Device version')}</dt><dd>{display(identity.firmware_version)}</dd><dt>{t('背景采集时间', 'Context collected at')}</dt><dd>{networkEvidence?.observed_at ?? 'N/A'}</dd><dt>{t('关联接口 / 资产数', 'Interfaces / inventory records')}</dt><dd>{Array.isArray(network.interfaces) ? network.interfaces.length : 'N/A'} / {display(network.inventory_count)}</dd></dl>{networkEvidence ? <details><summary>{t('完整背景快照', 'Full context snapshot')}</summary><pre>{JSON.stringify(network, null, 2)}</pre></details> : <p>{t('当前案件没有可用的网络背景快照，保留事件事实并等待补充取证。', 'The current case has no available network context snapshot. Its incident facts remain available while further evidence is collected.')}</p>}</section>
      </div>
      <section><div className="ci-section-header"><h2>{t('调查会话与判定依据', 'Investigation session and supporting evidence')}</h2>{sessionIds.length ? <label>{t('会话', 'Session')} <select value={sessionId} onChange={event => setChoice({ caseId: record.caseId, sessionId: event.target.value })}>{sessionIds.map(id => <option key={id} value={id}>{id}</option>)}</select></label> : null}</div>
        {!sessionIds.length ? <p>{t('案件已保存源事实，尚未建立调查会话。', 'Source facts are saved; an investigation session has not yet been created.')}</p> : null}
        {sessionError?.id === sessionId ? <p role="alert">{t('会话读取失败；上方保留案件事实与决定', 'Session unavailable; case facts and decision remain above')}: {sessionError?.message}</p> : null}
        {sessionId && !session && sessionError?.id !== sessionId ? <p role="status">{t('读取本案调查证据…', 'Loading evidence for this case…')}</p> : null}
        {evidence.map(item => { const measured = parsedOutput(item.output); const caption = decision?.evidence.find(ref => ref.evidenceId === item.evidence_id)?.label; return <article key={item.evidence_id} className="ci-evidence"><h3><b>{item.evidence_id}</b> · {decisiveIds.has(item.evidence_id) ? t('判定引用', 'Cited in decision') + ' · ' : ''}{caption ? prose(caption) : <code>{item.command}</code>} · {item.ok ? t('已采集', 'Collected') : t('采集失败', 'Collection failed')}</h3><small>{item.observed_at} · {item.source}</small>{evidenceProse(item.output) ? <p>{prose(evidenceProse(item.output))}</p> : null}{typeof measured.failed_logins === 'number' ? <p>{t('认证窗口统计', 'Authentication window')}: {measured.failed_logins} {t('次失败登录', 'failed logins')} · {display(measured.distinct_sources)} {t('个来源', 'sources')} · {display(measured.lockouts)} {t('次锁定', 'lockouts')}<br/>{display(measured.first_seen)} → {display(measured.last_seen)}</p> : null}<details><summary>{t('原始证据与命令', 'Raw evidence and command')}</summary><code>{item.command}</code><pre>{item.output}</pre></details></article> })}
        {!session && decision?.evidence.map(item => <article key={item.evidenceId} className="ci-evidence"><h3>{item.evidenceId} · {prose(item.label)}</h3>{evidenceProse(item.value) ? <p>{prose(evidenceProse(item.value))}</p> : null}<details><summary>{t('原始证据', 'Raw evidence')}</summary><pre>{item.value}</pre></details></article>)}
      </section>
      <div className="ci-grid"><section><h2>{t('当前工作集', 'Current working set')}</h2>{hypotheses.map(key => { const rows = list(workingSet[key]); return rows.length ? <div key={key}><h3>{({ confirmed_hypotheses: t('已确认判断', 'Confirmed hypotheses'), open_hypotheses: t('待验证判断', 'Open hypotheses'), rejected_hypotheses: t('已排除判断', 'Rejected hypotheses') })[key]}</h3>{rows.map((item, index) => <p key={index}>{prose(display(item.statement))} <code>{display(item.supporting_evidence_ids)}</code></p>)}</div> : null })}{!Object.keys(workingSet).length ? <p>{t('本会话尚未保存工作集。', 'No working set has been saved for this session.')}</p> : <details><summary>{t('完整工作集', 'Full working set')}</summary><pre>{JSON.stringify(workingSet, null, 2)}</pre></details>}</section><section><h2>{t('待补证据与后续步骤', 'Evidence gaps and next steps')}</h2>{decision?.missingObservations.length ? decision.missingObservations.map(item => <p key={item.code}>{prose(item.question)}{item.probe ? <code>{item.probe}</code> : null}</p>) : <p>{t('案件决定中没有登记待补观测。', 'The case decision records no outstanding observations.')}</p>}<p>{prose(decision?.nextProbe || decision?.disposition || t('等待调查更新。', 'Awaiting investigation update.'))}</p><h3>{t('执行后回读', 'Post-action readback')}</h3>{decision?.readback ? <details><summary>{t('原始回读记录', 'Raw readback record')}</summary><pre>{JSON.stringify(decision.readback, null, 2)}</pre></details> : <p>{t('尚未保存执行后回读。', 'No post-action readback has been saved.')}</p>}</section></div>
      <details className="ci-timeline"><summary>{t('完整案件过程', 'Full case timeline')} · {record.timeline.length}</summary>{record.timeline.map((event, index) => <details key={String(event.eventId ?? index)}><summary>{display(event.ts)} · {display(event.kind)}</summary><pre>{JSON.stringify(event, null, 2)}</pre></details>)}</details>
    </> : null}
  </main>
}
