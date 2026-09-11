import { useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import { useInvestigationCase } from './investigation-case'
import { caseSessionIds, type CaseSession } from './case-investigation-data'
import { useSentinelChain } from './use-sentinel-chain'
import { buildIncidentNarrative } from './incident-narrative'
import './case-investigation.css'

const value = (input: unknown): Record<string, unknown> => input && typeof input === 'object' && !Array.isArray(input) ? input as Record<string, unknown> : {}

export function CaseInvestigationPage({ lang, caseId, subject, onBack }: {
  lang: Lang
  caseId?: string
  subject?: string
  onBack?: () => void
}) {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const { record, error, refresh } = useInvestigationCase(caseId, 2_000)
  const chain = useSentinelChain(record?.subject ?? subject, 2_000)
  const [session, setSession] = useState<CaseSession | null>(null)
  const [sessionError, setSessionError] = useState('')

  const sessionId = useMemo(() => {
    if (!record) return undefined
    const comparison = [...record.timeline].reverse().find(event => event.kind === 'memory_comparison_recorded')
    const payload = value(comparison?.payload)
    return String(payload.sessionId ?? comparison?.sessionId ?? caseSessionIds(record)[0] ?? '') || undefined
  }, [record])

  useEffect(() => {
    if (!sessionId) { setSession(null); setSessionError(''); return }
    let alive = true
    const abort = new AbortController()
    const load = async () => {
      try {
        const response = await fetch(`/api/rca/investigate/${encodeURIComponent(sessionId)}`, { signal: abort.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const next = await response.json() as CaseSession
        if (next.session_id !== sessionId) throw new Error('Session identity mismatch')
        if (alive) { setSession(next); setSessionError('') }
      } catch (caught) {
        if (alive && !abort.signal.aborted) setSessionError(caught instanceof Error ? caught.message : String(caught))
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3_000)
    return () => { alive = false; abort.abort(); window.clearInterval(timer) }
  }, [sessionId])

  const narrative = record ? buildIncidentNarrative({ caseRecord: record, session, chain, lang }) : null
  const controlled = record?.sourcePayload?.dataClassification === 'controlled_test'
  const status = record?.status ?? 'open'
  const statusText: Record<string, string> = zh
    ? { open: '待调查', investigating: '调查中', waiting: '等待证据', escalated: '需人工处理', resolved: '已解决' }
    : { open: 'Open', investigating: 'Investigating', waiting: 'Waiting', escalated: 'Needs operator', resolved: 'Resolved' }

  return <main className="case-investigation">
    <header className="ci-header">
      <div className="ci-header-actions">
        {onBack ? <button onClick={onBack}>← {t('返回事件中心', 'Back to incident centre')}</button> : null}
        <button onClick={refresh}>{t('刷新', 'Refresh')}</button>
      </div>
      <div>
        <span className={`ci-status is-${status}`}>{statusText[status] ?? status}</span>
        {controlled ? <span className="ci-demo">{t('受控演示', 'Controlled demo')}</span> : null}
        <h1>{record?.businessDecision?.headline ?? record?.summary ?? subject ?? t('事件详情', 'Incident detail')}</h1>
        <p>{record ? `${record.subject} · ${t('首次发现', 'first seen')} ${new Date(record.firstSeenAt).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-GB', { hour12: false })}` : t('正在读取事件…', 'Loading incident…')}</p>
      </div>
    </header>

    {error ? <p className="ci-error" role="alert">{t('事件读取失败', 'Incident unavailable')}: {error}</p> : null}
    {!record && !error ? <div className="ci-loading"><span className="orbit" />{t('正在读取事件因果链…', 'Loading incident causal chain…')}</div> : null}

    {narrative ? <>
      <section className="ci-chain" aria-label={t('事件因果链', 'Incident causal chain')}>
        {narrative.stages.map((stage, index) => <article key={stage.id} className={`ci-stage is-${stage.state}`}>
          <div className="ci-stage-head"><span>{String(index + 1).padStart(2, '0')}</span><h2>{stage.title}</h2><i>{stage.state === 'complete' ? '✓' : stage.state === 'failed' ? '!' : '·'}</i></div>
          <dl>
            <div><dt>{t('发生了什么', 'What happened')}</dt><dd>{stage.happened}</dd></div>
            <div><dt>{t('依据是什么', 'Evidence')}</dt><dd>{stage.evidence}</dd></div>
            <div><dt>{t('为何这样决定', 'Why')}</dt><dd>{stage.reason}</dd></div>
            <div><dt>{t('结果是什么', 'Result')}</dt><dd>{stage.result}</dd></div>
          </dl>
        </article>)}
      </section>

      {narrative.memory ? <section className="ci-memory">
        <div><small>{t('本案记忆作用', 'MEMORY IMPACT')}</small><h2>{narrative.memory.applied ? t('历史经验已采用', 'Prior experience adopted') : t('已检索，未采用', 'Retrieved, not adopted')}</h2><p>{narrative.memory.summary ?? narrative.memory.memoryId}</p></div>
        <dl>
          <div><dt>{t('历史调查', 'Source investigation')}</dt><dd>{narrative.memory.sourceSessionId ?? t('待记录', 'Pending')}</dd></div>
          <div><dt>{t('匹配原因', 'Why it matched')}</dt><dd>{narrative.memory.matchedOn.join('、') || t('对象与故障族一致', 'Same subject and fault family')}</dd></div>
          <div><dt>{t('无记忆检查', 'Without memory')}</dt><dd><b>{narrative.memory.baselineChecks}</b> {t('项', 'checks')}</dd></div>
          <div><dt>{t('实际检查', 'Executed')}</dt><dd><b>{narrative.memory.executedChecks}</b> {t('项', 'checks')}</dd></div>
          <div className="ci-memory-saved"><dt>{t('实际变化', 'Actual change')}</dt><dd><b>-{narrative.memory.savedChecks}</b> {t('项检查', 'checks')}{narrative.memory.baselineSeconds != null && narrative.memory.executedSeconds != null ? <small>{t('耗时', 'time')} {narrative.memory.baselineSeconds.toFixed(1)}→{narrative.memory.executedSeconds.toFixed(1)}s ({narrative.memory.baselineSeconds >= narrative.memory.executedSeconds ? t(`减少 ${(narrative.memory.baselineSeconds - narrative.memory.executedSeconds).toFixed(1)}s`, `${(narrative.memory.baselineSeconds - narrative.memory.executedSeconds).toFixed(1)}s saved`) : t(`增加 ${(narrative.memory.executedSeconds - narrative.memory.baselineSeconds).toFixed(1)}s`, `${(narrative.memory.executedSeconds - narrative.memory.baselineSeconds).toFixed(1)}s slower`)})</small> : null}</dd></div>
        </dl>
      </section> : null}

      {sessionError ? <p className="ci-error" role="alert">{t('调查会话暂时不可用', 'Investigation session unavailable')}: {sessionError}</p> : null}
      <details className="ci-audit">
        <summary>{t('技术审计记录', 'Technical audit record')} <small>{t('命令、内部字段、原始记录', 'commands, internal fields, raw records')}</small></summary>
        <h3>{t('命令回执', 'Command receipts')}</h3>
        <pre>{JSON.stringify(narrative.audit.commands, null, 2)}</pre>
        <h3>{t('案件原始记录', 'Raw case record')}</h3>
        <pre>{JSON.stringify(narrative.audit.caseRecord, null, 2)}</pre>
        {narrative.audit.session ? <><h3>{t('调查会话原始记录', 'Raw investigation session')}</h3><pre>{JSON.stringify(narrative.audit.session, null, 2)}</pre></> : null}
      </details>
    </> : null}
  </main>
}
