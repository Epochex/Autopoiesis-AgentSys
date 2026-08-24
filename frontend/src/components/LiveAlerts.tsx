import './live-alerts.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import type { TheaterEvent } from '../types'
import { sentinelStageIds } from './netops-pipeline'
import { latestIncidentCycle } from './sentinel-cycle'

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
  phase: 'escalated' | 'watching' | 'resolved' | 'needs_human' | 'reported' | 'declined' | 'cooling' | 'detected'
  action: string | null
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
}

const SUBJECT_KINDS = new Set([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved', 'escalated',
  'escalation_cleared',
])

const PHASE_LABEL: Record<Row['phase'], [string, string]> = {
  detected: ['检测待确认', 'DETECTION PENDING'],
  watching: ['处置观察中', 'ACTION UNDER OBSERVATION'],
  resolved: ['处置已验证', 'ACTION VERIFIED'],
  escalated: ['升级人工处置', 'ESCALATED TO OPERATOR'],
  needs_human: ['待人工复核', 'OPERATOR REVIEW'],
  reported: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  declined: ['安全门未放行', 'SAFETY GATE BLOCKED'],
  cooling: ['冷却中', 'COOLING DOWN'],
}

/** Anything older than this is history, not a live alert. */
const RECENT_MS = 30 * 60 * 1000

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
    anchorIp: suggestion?.anchorIp ?? undefined,
    originIp: suggestion?.originIp ?? undefined,
    blastScope: suggestion?.impactLevel,
    blastSummary: suggestion?.stageTelemetry?.find((stage) => stage.stageId === 'preflight')?.detail,
    stageIds: sentinelStageIds(timeline),
  }
}

function summarise(events: Record<string, unknown>[], suggestions: SituationSuggestion[], zh: boolean): Row[] {
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
    if (Date.parse(at) < cutoff) continue

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
    else if (remediated?.needs_human) phase = 'needs_human'
    else if (kinds.includes('no_safe_action')) phase = 'reported'
    else if (kinds.includes('declined')) phase = 'declined'
    else if (kinds.includes('cooldown')) phase = 'cooling'
    else if (kinds.includes('preflight')) phase = 'watching'

    const detection = [...chain].reverse().find((e) => e.kind === 'detected')
    const resolved = [...chain].reverse().find((e) => e.kind === 'resolved')
    const base: Omit<Row, 'theater'> = {
      subject,
      severity: String(detection?.severity ?? 'high'),
      summary: phase === 'reported'
        ? reportSummary(subject, noAction, zh)
        : phase === 'escalated'
          ? escalationSummary(detection, escalated, zh)
          : phase === 'resolved'
            ? resolvedSummary(subject, resolved, remediated, zh)
        : String(detection?.summary ?? ''),
      at,
      phase,
      action: (detection?.action as string | null) ?? null,
    }
    const suggestion = suggestions.find((item) => item.deviceKey === subject || item.device === subject)
    rows.push({ ...base, theater: theaterEvent(subject, base, chain, suggestion) })
  }
  // Operator-owned and active records sort ahead of closed records.
  const order: Record<Row['phase'], number> = {
    escalated: 0, needs_human: 1, watching: 2, detected: 3,
    cooling: 4, declined: 4, reported: 4, resolved: 5,
  }
  rows.sort((a, b) => order[a.phase] - order[b.phase] || (a.at < b.at ? 1 : -1))
  return rows
}

export function LiveAlerts({
  lang,
  onOpen,
  theaterActive = false,
  activeSubject,
}: {
  lang: Lang
  onOpen: (subject: string, theater: TheaterEvent) => void
  theaterActive?: boolean
  activeSubject?: string
}) {
  const zh = lang === 'zh'
  const [rows, setRows] = useState<Row[]>([])
  const inFlight = useRef(false)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const response = await fetch('/api/rca/sentinel/timeline?limit=400')
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
      setRows(summarise(body.events ?? [], suggestions, zh))
    } catch {
      // A dead endpoint must not blank the page underneath; keep the last view.
    } finally {
      inFlight.current = false
    }
  }, [zh])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  if (!rows.length) return null

  const stopped = rows.filter((r) => r.phase === 'escalated').length
  const live = rows.filter((r) => r.phase === 'detected' || r.phase === 'watching' || r.phase === 'needs_human').length
  const held = rows.filter((r) => r.phase === 'reported' || r.phase === 'declined' || r.phase === 'cooling').length
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
          {counts.length ? counts.join(' · ') : (zh ? '当前记录均已闭环' : 'all current records closed')}
        </span>
        <span className="la-hint">
          {theaterActive
            ? (zh ? '选择记录切换当前剧场 ▸' : 'select a record to switch the current theater ▸')
            : (zh ? '选择记录查看证据与决策链 ▸' : 'select a record for evidence and decision chain ▸')}
        </span>
      </div>
      <ul className="la-rows">
        {rows.slice(0, 6).map((row) => (
          <li key={row.subject}>
            <button
              type="button"
              className={`la-row p-${row.phase} sv-${row.severity}${activeSubject === row.subject ? ' is-active' : ''}`}
              aria-current={activeSubject === row.subject ? 'true' : undefined}
              onClick={() => onOpen(row.subject, row.theater)}
            >
              <span className="la-phase">{PHASE_LABEL[row.phase][zh ? 0 : 1]}</span>
              <span className="la-subject">{row.subject}</span>
              <span className="la-summary">{row.summary}</span>
              <span className="la-at">{row.at.slice(11, 19)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
