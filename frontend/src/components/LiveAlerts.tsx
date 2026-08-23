import './live-alerts.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { latestIncidentCycle } from './sentinel-cycle'

/* ── 实时告警条 — what the sentinel found, on the page people watch ──────────
 *
 * The situational page draws the FortiGate corpus, which is a fixed historical
 * window. Anything the sentinel notices right now happens on a different data
 * path entirely, so a fault could be detected, acted on and closed without this
 * page changing at all — which is exactly what it looked like.
 *
 * This strip is the missing link. It only appears when there is something to
 * say, and clicking a row goes to that subject's chain rather than to a
 * generic page. */

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
])

const PHASE_LABEL: Record<Row['phase'], [string, string]> = {
  detected: ['刚发现', 'DETECTED'],
  watching: ['处置中', 'IN FLIGHT'],
  resolved: ['已自动处置', 'HANDLED AUTOMATICALLY'],
  // Both ask for a person, so both say so. They are not the same ask: needs_human
  // is one attempt that went wrong, escalated is the system declining to try at
  // all because it has already fixed this three times. The row's summary and the
  // theater card carry that difference.
  // Distinct from needs_human on purpose: that one means a revert could not
  // be verified, this one means the system decided to stop repairing.
  escalated: ['不再自动修', 'STOPPED REPAIRING'],
  needs_human: ['要人工', 'NEEDS A PERSON'],
  reported: ['只报不动', 'REPORTED ONLY'],
  declined: ['安全门拒绝', 'DECLINED BY GATE'],
  cooling: ['冷却中', 'COOLING DOWN'],
}

/** Anything older than this is history, not a live alert. */
const RECENT_MS = 30 * 60 * 1000

function summarise(events: Record<string, unknown>[]): Row[] {
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
    let phase: Row['phase'] = 'detected'
    // Escalation is read first: the chain of a subject that keeps coming back
    // still holds every fix that worked, and the newest of those would otherwise
    // paint the row 已自愈 on the incident nobody is dealing with.
    if (kinds.includes('escalated')) phase = 'escalated'
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
      summary: String(detection?.summary ?? ''),
      at,
      phase,
      action: (detection?.action as string | null) ?? null,
    })
  }
  // Anything still moving goes first; a healed incident is reassurance, not news.
  // Escalated outranks even that: it is the only row where the system has
  // stopped, so nothing else will move it until someone does.
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
      setRows(summarise(body.events ?? []))
    } catch {
      // A dead endpoint must not blank the page underneath; keep the last view.
    } finally {
      inFlight.current = false
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  if (!rows.length) return null

  // An escalated incident is not being handled — counting it as 处理中 would tell
  // the operator the system has it when the whole point is that it has stopped.
  const stopped = rows.filter((r) => r.phase === 'escalated').length
  const live = rows.filter((r) => r.phase === 'detected' || r.phase === 'watching' || r.phase === 'needs_human').length
  const held = rows.filter((r) => r.phase === 'reported' || r.phase === 'declined' || r.phase === 'cooling').length
  const counts = [
    stopped ? (zh ? `${stopped} 项要人工` : `${stopped} need a person`) : '',
    live ? (zh ? `${live} 项处理中` : `${live} in flight`) : '',
    held ? (zh ? `${held} 项未执行写操作` : `${held} held without writes`) : '',
  ].filter(Boolean)

  return (
    <div className="la">
      <div className="la-head">
        <span className="la-k">{zh ? '系统自己发现的' : 'FOUND BY THE SYSTEM'}</span>
        <span className="la-count">
          {counts.length ? counts.join(' · ') : (zh ? '都已自动处置' : 'all handled automatically')}
        </span>
        <span className="la-hint">{zh ? '点一条看完整处置链路 ▸' : 'click a row for its chain ▸'}</span>
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
