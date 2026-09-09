import './live-alerts.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import type { TheaterEvent } from '../types'
import { sentinelStageIds } from './autopoiesis-pipeline'
import { latestIncidentCycle } from './sentinel-cycle'
import { completedEventAction, matchCompletedEvent, type CompletedEvent } from './completed-events'
import { useDisplayTranslation } from './useDisplayTranslation'

/* Real-time incident queue for events emitted by the sentinel.
 *
 * The situational page draws the FortiGate corpus, which is a fixed historical
 * window. Current sentinel events arrive through a separate data path.
 *
 * The strip appears for actionable records and opens the selected subject's
 * evidence and decision chain. */

interface Row {
  subject: string
  severity: string
  summary: string
  at: string
  cycleOpenedAt: string
  phase: 'escalated' | 'watching' | 'resolved' | 'needs_human' | 'reported' | 'declined' | 'cooling' | 'detected' | 'cleared'
  action: string | null
  caseId?: string
  controlled: boolean
  theater: TheaterEvent
}

interface SituationSuggestion {
  id?: string
  ts?: string
  scope?: string
  severity?: string
  priority?: string
  summary?: string
  device?: string
  deviceKey?: string
  anchorIp?: string | null
  originIp?: string | null
  impactLevel?: string
  timeline?: { kind?: string }[]
  stageTelemetry?: { stageId?: string; detail?: string }[]
  caseId?: string
  incidentFacts?: { dataClassification?: string }
}

const SUBJECT_KINDS = new Set([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved', 'escalated',
  'escalation_cleared', 'condition_cleared',
])

const PHASE_LABEL: Record<Row['phase'], [string, string]> = {
  detected: ['检测待确认', 'DETECTION PENDING'],
  watching: ['处置观察中', 'ACTION UNDER OBSERVATION'],
  resolved: ['处置已验证', 'ACTION VERIFIED'],
  cleared: ['状态已恢复', 'CONDITION CLEARED'],
  escalated: ['升级人工处置', 'ESCALATED TO OPERATOR'],
  needs_human: ['待人工复核', 'OPERATOR REVIEW'],
  reported: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  declined: ['安全门未放行', 'SAFETY GATE BLOCKED'],
  cooling: ['冷却中', 'COOLING DOWN'],
}

/** Anything older than this is history, not a live alert. */
const RECENT_MS = 30 * 60 * 1000
/** A refused transient condition should leave the live queue soon after its
 * detector stops refreshing it.  The append-only event remains available in
 * the incident theater and audit views. */
const HELD_RECENT_MS = 2 * 60 * 1000

function reportSummary(subject: string, refusal: Record<string, unknown> | undefined, zh: boolean): string {
  const recorded = String(refusal?.reason ?? '').trim()
  if (/^(192\.0\.2|198\.51\.100|203\.0\.113)\./.test(subject)) {
    return zh
      ? '安全门未放行临时封禁：来源归属未确认，管理地址豁免、封禁 TTL、提交后回读和超时回滚条件未齐；后续由安全运营核验并处置。'
      : 'Temporary block not authorized: source ownership is unconfirmed and management exemptions, block TTL, post-commit readback, and timed rollback are incomplete; Security Operations owns validation and response.'
  }
  if (recorded) return recorded
  return zh
    ? '写操作未授权：安全门条件未满足；检测证据已记录，后续由值班人员核验并处置。'
    : 'Write not authorized: safety-gate conditions were not met; evidence is recorded for operator validation and response.'
}

function escalationSummary(
  detection: Record<string, unknown> | undefined,
  escalation: Record<string, unknown> | undefined,
  zh: boolean,
): string {
  const recurrences = Number(escalation?.recurrences ?? 0)
  const windowHours = Number(escalation?.window_hours ?? 0)
  const current = String(detection?.summary ?? '').trim()
  const currentSentence = current.replace(/[。.!！]+$/, '')
  if (zh) {
    const history = recurrences > 0
      ? `${windowHours || 24} 小时内已有 ${recurrences} 轮处置通过回读后再次复发`
      : '同一故障在处置后再次复发'
    return `复发升级：${history}；${currentSentence || '当前故障仍成立'}。重复动作预算已用尽，转人工排查持续性原因。`
  }
  const history = recurrences > 0
    ? `${recurrences} verified recoveries recurred within ${windowHours || 24} hours`
    : 'the same fault recurred after remediation'
  return `Recurrence escalation: ${history}; ${currentSentence || 'the fault remains present'}. The repeat-action budget is exhausted and persistent-cause investigation is assigned to an operator.`
}

function resolvedSummary(
  subject: string,
  resolved: Record<string, unknown> | undefined,
  remediated: Record<string, unknown> | undefined,
  zh: boolean,
): string {
  const readback = String(resolved?.note ?? remediated?.detail ?? '').trim()
  if (zh) {
    return `${subject} 已完成处置与现场回读${readback ? `：${readback}` : '，恢复状态已验证'}。`
  }
  return `${subject} completed remediation and live readback${readback ? `: ${readback}` : '; recovery is verified'}.`
}

function theaterEvent(
  subject: string,
  row: Omit<Row, 'theater'>,
  chain: Record<string, unknown>[],
  suggestion?: SituationSuggestion,
): TheaterEvent {
  const timeline = suggestion?.timeline?.map((step) => ({ kind: String(step.kind ?? '') }))
    ?? chain.map((step) => ({ kind: String(step.kind ?? '') }))
  return {
    kind: 'suggestion',
    id: String(suggestion?.id ?? `sentinel-live-${subject}-${row.at}`),
    ts: String(suggestion?.ts ?? row.at),
    device: String(suggestion?.deviceKey ?? suggestion?.device ?? subject),
    deviceLabel: String(suggestion?.device ?? subject),
    severity: String(suggestion?.severity ?? row.severity),
    priority: suggestion?.priority,
    summary: row.summary,
    scope: String(suggestion?.scope ?? 'sentinel'),
    caseId: suggestion?.caseId,
    anchorIp: suggestion?.anchorIp ?? undefined,
    originIp: suggestion?.originIp ?? undefined,
    blastScope: suggestion?.impactLevel,
    blastSummary: suggestion?.impactLevel,
    stageIds: sentinelStageIds(timeline),
    dataClassification: row.controlled ? 'controlled_test' : 'production_observed',
  }
}

function summarise(
  events: Record<string, unknown>[],
  suggestions: SituationSuggestion[],
  zh: boolean,
  pinnedSubject?: string,
): Row[] {
  const bySubject = new Map<string, Record<string, unknown>[]>()
  for (const event of events) {
    const kind = String(event.kind ?? '')
    if (!SUBJECT_KINDS.has(kind)) continue
    const subject = String(event.subject ?? '')
    if (!subject) continue
    const bucket = bySubject.get(subject)
    if (bucket) bucket.push(event)
    else bySubject.set(subject, [event])
  }

  const rows: Row[] = []
  const cutoff = Date.now() - RECENT_MS
  for (const [subject, history] of bySubject) {
    const chain = latestIncidentCycle(history)
    const last = chain[chain.length - 1]
    const at = String(last.at ?? '')
    if (subject !== pinnedSubject && Date.parse(at) < cutoff) continue

    const kinds = chain.map((e) => String(e.kind))
    const remediated = [...chain].reverse().find((e) => e.kind === 'remediated')
    const noAction = [...chain].reverse().find((e) => e.kind === 'no_safe_action')
    const escalated = [...chain].reverse().find((e) => e.kind === 'escalated')
    const escalationCleared = [...chain].reverse().find((e) => e.kind === 'escalation_cleared')
    const escalationActive = Boolean(escalated) && (
      !escalationCleared || String(escalationCleared.at ?? '') < String(escalated?.at ?? '')
    )
    let phase: Row['phase'] = 'detected'
    // Escalation takes precedence over successful outcomes from earlier cycles.
    if (escalationActive) phase = 'escalated'
    else if (kinds.includes('resolved')) phase = 'resolved'
    else if (kinds.includes('condition_cleared')) phase = 'cleared'
    else if (remediated?.needs_human) phase = 'needs_human'
    else if (kinds.includes('no_safe_action')) phase = 'reported'
    else if (kinds.includes('declined')) phase = 'declined'
    else if (kinds.includes('cooldown')) phase = 'cooling'
    else if (kinds.includes('preflight')) phase = 'watching'

    if (
      (phase === 'declined' || phase === 'cooling')
      && subject !== pinnedSubject
      && Date.parse(at) < Date.now() - HELD_RECENT_MS
    ) continue

    const detection = [...chain].reverse().find((e) => e.kind === 'detected')
    const resolved = [...chain].reverse().find((e) => e.kind === 'resolved')
    const suggestion = suggestions.find((item) => item.deviceKey === subject || item.device === subject)
    const controlled = suggestion?.incidentFacts?.dataClassification === 'controlled_test'
      || subject.startsWith('autopoiesis-acceptance-')
      || chain.some((event) => Boolean(event.demoRunId))
    const base: Omit<Row, 'theater'> = {
      subject,
      severity: String(detection?.severity ?? 'high'),
      summary: phase === 'cleared' ? (zh ? '最新观测显示故障条件已消失；本轮未确认处置动作的恢复贡献。' : 'The latest observation shows the condition has cleared; recovery attribution to an action is unverified.') : phase === 'reported'
        ? reportSummary(subject, noAction, zh)
        : phase === 'escalated'
          ? escalationSummary(detection, escalated, zh)
          : phase === 'resolved'
            ? resolvedSummary(subject, resolved, remediated, zh)
        : String(detection?.summary ?? ''),
      at,
      cycleOpenedAt: String(detection?.cycle_opened_at ?? chain.find(event => event.kind === 'detected')?.at ?? chain[0]?.at ?? ''),
      phase,
      action: (detection?.action as string | null) ?? null,
      controlled,
    }
    rows.push({
      ...base,
      caseId: suggestion?.caseId,
      theater: theaterEvent(subject, base, chain, suggestion),
    })
  }
  // Operator-owned and active records sort ahead of closed records.
  const order: Record<Row['phase'], number> = {
    escalated: 0, needs_human: 1, watching: 2, detected: 3,
    cooling: 4, declined: 4, reported: 4, resolved: 5, cleared: 5,
  }
  rows.sort((a, b) => order[a.phase] - order[b.phase] || (a.at < b.at ? 1 : -1))
  if (pinnedSubject) {
    rows.sort((a, b) => Number(b.subject === pinnedSubject) - Number(a.subject === pinnedSubject))
  }
  return rows
}

export function LiveAlerts({
  lang,
  onOpen,
  theaterActive = false,
  activeSubject,
  autoOpenSubject,
  onArchived,
}: {
  lang: Lang
  onOpen: (subject: string, theater: TheaterEvent, caseId?: string, origin?: 'operator' | 'focus') => void
  theaterActive?: boolean
  activeSubject?: string
  autoOpenSubject?: string
  onArchived?: (subject: string) => void
}) {
  const zh = lang === 'zh'
  const [rows, setRows] = useState<Row[]>([])
  const [completed, setCompleted] = useState<CompletedEvent[]>([])
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [actionError, setActionError] = useState('')
  const { translate } = useDisplayTranslation([actionError], lang)
  const inFlight = useRef(false)
  const autoOpened = useRef<string | null>(null)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [response, lifecycle] = await Promise.all([fetch('/api/rca/sentinel/timeline?limit=400'), fetch('/api/rca/completed-events').then(async r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return await r.json() as {records: CompletedEvent[]} }).catch(() => null)])
      if (lifecycle) setCompleted(lifecycle.records)
      const body = (await response.json()) as { events?: Record<string, unknown>[] }
      let suggestions: SituationSuggestion[] = []
      try {
        const situationResponse = await fetch(`/api/rca/live-situation?lang=${zh ? 'zh' : 'en'}`)
        const situation = (await situationResponse.json()) as { suggestions?: SituationSuggestion[] }
        suggestions = situation.suggestions ?? []
      } catch {
        // The append-only timeline still provides a complete switch target while
        // the richer projection is briefly unavailable.
      }
      setRows(summarise(body.events ?? [], suggestions, zh, autoOpenSubject))
    } catch {
      // A dead endpoint must not blank the page underneath; keep the last view.
    } finally {
      inFlight.current = false
    }
  }, [autoOpenSubject, zh])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  useEffect(() => {
    if (!autoOpenSubject || activeSubject === autoOpenSubject) return
    const row = rows.find((item) => item.subject === autoOpenSubject && !matchCompletedEvent(completed, item.subject, item.cycleOpenedAt)?.archived)
    if (!row) return
    const key = `${row.subject}:${row.at}`
    if (autoOpened.current === key) return
    autoOpened.current = key
    onOpen(row.subject, row.theater, row.caseId, 'focus')
  }, [activeSubject, autoOpenSubject, completed, onOpen, rows])

  const visibleRows = rows.flatMap(row => {
    const record = matchCompletedEvent(completed, row.subject, row.cycleOpenedAt)
    if (record?.archived) return []
    if (record?.completionState === 'in_progress' && row.phase === 'resolved') {
      const summary = zh ? '处置回读或观察证据尚未齐全，仍需完成验证。' : 'Action readback or observation evidence is incomplete; verification is still required.'
      return [{...row, phase: 'needs_human' as const, summary, theater: {...row.theater, summary}}]
    }
    return [row]
  })
  const act = async (action: 'archive' | 'reflect', row: Row, record: CompletedEvent) => {
    setBusy(record.recordId); setActionError(''); setNotice('')
    try {
      const result = await completedEventAction(action, record.recordId)
      setCompleted(current => current.map(item => item.recordId === record.recordId ? {...item, ...(result.record ?? {}), ...(action === 'archive' ? {archived: true} : {memoryId: result.memoryId ?? result.record?.memoryId})} : item))
      setNotice(action === 'archive' ? (zh ? '已从当前列表删除，历史审计保留。' : 'Removed from this list; historical audit records are retained.') : (result.retrievalReady === false ? (zh ? '已将受控演示写入长期记忆隔离记录，可在长期记忆页查看；生产调查不会自动召回这条演示经验。' : 'Saved this controlled demonstration to quarantined long-term memory. It is visible on the memory page and excluded from automatic production recall.') : (zh ? '已将处置、验证证据与适用条件写入长期记忆，可在长期记忆页查看并供后续调查检索。' : 'Saved the action, verification evidence and applicability to long-term memory for future investigation recall.')))
      if (action === 'archive') onArchived?.(row.subject)
      await load()
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)) }
    finally { setBusy('') }
  }

  if (!visibleRows.length && !notice && !actionError) return null

  const productionRows = visibleRows.filter((row) => !row.controlled)
  const demoRows = visibleRows.filter((row) => row.controlled)
  const stopped = productionRows.filter((r) => r.phase === 'escalated').length
  const live = productionRows.filter((r) => r.phase === 'detected' || r.phase === 'watching' || r.phase === 'needs_human').length
  const held = productionRows.filter((r) => r.phase === 'reported' || r.phase === 'declined' || r.phase === 'cooling').length
  const counts = [
    stopped ? (zh ? `${stopped} 项已升级人工` : `${stopped} escalated`) : '',
    live ? (zh ? `${live} 项处理中` : `${live} in flight`) : '',
    held ? (zh ? `${held} 项写操作未授权` : `${held} writes not authorized`) : '',
  ].filter(Boolean)

  return (
    <div className="la">
      <div className="la-head">
        <span className="la-k">{zh ? '实时安全与故障事件' : 'LIVE SECURITY & FAULT EVENTS'}</span>
        <span className="la-count">
          {counts.length
            ? counts.join(' · ')
            : productionRows.length
              ? (zh ? '当前生产记录均已闭环' : 'all production records closed')
              : (zh ? '当前无待处置生产事件' : 'no production event requires action')}
        </span>
        {demoRows.length ? <span className="la-demo-count">{zh ? `${demoRows.length} 条受控演示` : `${demoRows.length} CONTROLLED DEMOS`}</span> : null}
        <span className="la-hint">
          {theaterActive
            ? (zh ? '选择记录切换当前剧场 ▸' : 'select a record to switch the current theater ▸')
            : (zh ? '选择记录查看证据与决策链 ▸' : 'select a record for evidence and decision chain ▸')}
        </span>
      </div>
      {notice ? <p className="la-feedback" role="status">{notice}</p> : null}
      {actionError ? <p className="la-feedback" role="alert">{translate(actionError)}</p> : null}
      <ul className="la-rows">
        {visibleRows.slice(0, 6).map((row) => (
          <li key={row.subject}>
            <button
              type="button"
              className={`la-row p-${row.phase} sv-${row.severity}${activeSubject === row.subject ? ' is-active' : ''}`}
              aria-current={activeSubject === row.subject ? 'true' : undefined}
              onClick={() => onOpen(row.subject, row.theater, row.caseId, 'operator')}
            >
              <span className="la-phase">{PHASE_LABEL[row.phase][zh ? 0 : 1]}</span>
              <span className={`la-origin ${row.controlled ? 'is-demo' : ''}`}>{row.controlled ? (zh ? '受控演示' : 'DEMO') : (zh ? '生产观测' : 'PRODUCTION')}</span>
              <span className="la-subject">{row.subject}</span>
              <span className="la-summary">{row.summary}</span>
              <span className="la-at">{row.at.slice(11, 19)}</span>
            </button>
            {(() => {
              const record = matchCompletedEvent(completed, row.subject, row.cycleOpenedAt)
              if (!record?.canArchive && !record?.canReflect) return null
              return <div className="la-actions">
                {record.canReflect ? <button disabled={Boolean(busy) || Boolean(record.memoryId)} onClick={() => void act('reflect', row, record)}>{busy === record.recordId ? (zh ? '处理中…' : 'Working…') : record.memoryId ? (zh ? '✓ 已写入长期记忆' : '✓ Saved to long-term memory') : (zh ? '反思并记忆' : 'Reflect & remember')}</button> : null}
                {record.canArchive ? <button disabled={Boolean(busy)} title={zh ? '从当前列表删除，保留历史证据与已保存记忆' : 'Remove from this list; retain historical evidence and saved memory'} onClick={() => void act('archive', row, record)}>{zh ? '删除' : 'Delete'}</button> : null}
                {record.memoryId ? <small>{zh ? '记忆' : 'Memory'} · {record.memoryId}</small> : null}
              </div>
            })()}
          </li>
        ))}
      </ul>
    </div>
  )
}
