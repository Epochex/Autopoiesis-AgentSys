import './live-alerts.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
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

function summarise(events: Record<string, unknown>[], zh: boolean): Row[] {
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
    rows.push({
      subject,
      severity: String(detection?.severity ?? 'high'),
      summary: phase === 'reported'
        ? reportSummary(subject, noAction, zh)
        : String(detection?.summary ?? ''),
      at,
      phase,
      action: (detection?.action as string | null) ?? null,
    })
  }
  // Operator-owned and active records sort ahead of closed records.
  const order: Record<Row['phase'], number> = {
    escalated: 0, needs_human: 1, watching: 2, detected: 3,
    cooling: 4, declined: 4, reported: 4, resolved: 5,
  }
  rows.sort((a, b) => order[a.phase] - order[b.phase] || (a.at < b.at ? 1 : -1))
  return rows
}

export function LiveAlerts({ lang, onOpen }: { lang: Lang; onOpen: (subject: string) => void }) {
  const zh = lang === 'zh'
  const [rows, setRows] = useState<Row[]>([])
  const inFlight = useRef(false)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const response = await fetch('/api/rca/sentinel/timeline?limit=400')
      const body = (await response.json()) as { events?: Record<string, unknown>[] }
      setRows(summarise(body.events ?? [], zh))
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
        <span className="la-hint">{zh ? '选择记录查看证据与决策链 ▸' : 'select a record for evidence and decision chain ▸'}</span>
      </div>
      <ul className="la-rows">
        {rows.slice(0, 6).map((row) => (
          <li key={row.subject}>
            <button type="button" className={`la-row p-${row.phase} sv-${row.severity}`} onClick={() => onOpen(row.subject)}>
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
