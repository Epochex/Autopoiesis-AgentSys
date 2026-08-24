import './sentinel.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { latestIncidentCycle } from './sentinel-cycle'

/* Event audit timeline from detection through disposition.
 *
 * Records are grouped by affected object. Quiet polling cycles are folded, while
 * safety-gate decisions and operator handoffs remain visible for audit.
 */

export type TimelineKind =
  | 'sentinel_started' | 'sentinel_stopped' | 'cycle' | 'cycle_failed'
  | 'detected' | 'awaiting_confirmation' | 'no_safe_action' | 'cooldown'
  | 'preflight' | 'declined' | 'remediated' | 'resolved' | 'detector_failed'
  | 'escalated' | 'escalation_cleared'

export interface BlastRadius {
  scope: string
  summary: string
  measured?: Record<string, unknown>
  reversible?: boolean | null
}

export interface TimelineEvent {
  at: string
  kind: TimelineKind
  subject?: string
  detector?: string
  family?: string
  severity?: string
  summary?: string
  note?: string
  action?: string | null
  target?: string | null
  streak?: number
  need?: number
  eligible?: boolean
  reason?: string
  blast_radius?: BlastRadius | null
  outcome?: string
  needs_human?: boolean
  detail?: string | null
  samples?: number
  baseline?: Record<string, boolean> | null
  remaining_sec?: number
  detections?: number
  acted?: number
  error?: string
  evidence?: Record<string, unknown>
}

type St =
  | { s: 'load' }
  | { s: 'err'; m: string }
  | { s: 'ok'; events: TimelineEvent[] }

/** One subject's chain, in the order it happened. */
interface Incident {
  subject: string
  events: TimelineEvent[]
  opened: string
  closed: string | null
  outcome: 'resolved' | 'needs_human' | 'declined' | 'watching' | 'reported' | 'cooling' | 'escalated'
}

const T = (zh: boolean) => ({
  title: zh ? '事件审计时间线' : 'INCIDENT AUDIT TIMELINE',
  lede: zh
    ? '按事件记录检测事实、安全门条件、候选动作、决策结果和人工交接。'
    : 'Incident records covering detection facts, safety conditions, candidate actions, decisions, and operator handoff.',
  live: zh ? '自动刷新' : 'AUTO REFRESH',
  refresh: zh ? '刷新' : 'REFRESH',
  poll: zh ? '立即巡检一轮' : 'POLL NOW',
  polling: zh ? '巡检中…' : 'POLLING…',
  updated: zh ? '上次刷新' : 'UPDATED',
  empty: zh ? '当前没有事件审计记录。' : 'NO INCIDENT AUDIT RECORDS.',
  loading: zh ? '读取中…' : 'LOADING…',
  quiet: zh ? '条巡检未发现异常，已折叠' : 'quiet cycles folded',
  showRaw: zh ? '看原始事件' : 'RAW EVENTS',
  hideRaw: zh ? '收起' : 'HIDE',
  radius: zh ? '影响面' : 'BLAST RADIUS',
  baseline: zh ? '动作前基线' : 'BASELINE',
  samples: zh ? '观察期采样' : 'WATCH SAMPLES',
})

const OUTCOME_LABEL: Record<Incident['outcome'], [string, string]> = {
  resolved: ['恢复已验证', 'RECOVERY VERIFIED'],
  needs_human: ['待人工复核', 'OPERATOR REVIEW'],
  declined: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  watching: ['处置进行中', 'RESPONSE IN PROGRESS'],
  reported: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  cooling: ['冷却中', 'COOLING DOWN'],
  escalated: ['已升级人工处置', 'ESCALATED TO OPERATOR'],
}

const STEP_LABEL: Record<TimelineKind, [string, string]> = {
  detected: ['检测事实', 'DETECTION FACT'],
  awaiting_confirmation: ['二次确认', 'SECOND CONFIRMATION'],
  no_safe_action: ['安全门未放行', 'SAFETY GATE BLOCKED'],
  cooldown: ['冷却中', 'COOLING DOWN'],
  preflight: ['安全门条件', 'SAFETY CONDITIONS'],
  declined: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  remediated: ['动作回执', 'ACTION RECEIPT'],
  resolved: ['恢复已验证', 'RECOVERY VERIFIED'],
  detector_failed: ['探测器报错', 'DETECTOR FAILED'],
  escalated: ['升级人工处置', 'ESCALATED TO OPERATOR'],
  escalation_cleared: ['升级状态解除', 'ESCALATION CLEARED'],
  cycle: ['巡检', 'CYCLE'],
  cycle_failed: ['巡检失败', 'CYCLE FAILED'],
  sentinel_started: ['哨兵启动', 'STARTED'],
  sentinel_stopped: ['哨兵停止', 'STOPPED'],
}

const pick = (pair: [string, string], zh: boolean) => (zh ? pair[0] : pair[1])
const clock = (iso: string) => (iso || '').slice(11, 19)

/** Steps that belong to a subject's chain rather than to the loop itself. */
const SUBJECT_KINDS = new Set<TimelineKind>([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved', 'escalated', 'escalation_cleared',
])

function group(events: TimelineEvent[]): { incidents: Incident[]; quiet: number } {
  const bySubject = new Map<string, TimelineEvent[]>()
  let quiet = 0
  for (const event of events) {
    if (event.kind === 'cycle') {
      if (!event.detections) quiet += 1
      continue
    }
    if (!SUBJECT_KINDS.has(event.kind)) continue
    const subject = event.subject || event.target || 'N/A'
    const bucket = bySubject.get(subject)
    if (bucket) bucket.push(event)
    else bySubject.set(subject, [event])
  }

  const incidents: Incident[] = []
  for (const [subject, history] of bySubject) {
    const chain = latestIncidentCycle(history)
    const last = chain[chain.length - 1]
    const remediated = [...chain].reverse().find((e) => e.kind === 'remediated')
    let outcome: Incident['outcome'] = 'watching'
    if (chain.some((e) => e.kind === 'escalated')) outcome = 'escalated'
    else if (chain.some((e) => e.kind === 'resolved')) outcome = 'resolved'
    else if (remediated?.needs_human) outcome = 'needs_human'
    else if (last.kind === 'no_safe_action') outcome = 'reported'
    else if (last.kind === 'cooldown') outcome = 'cooling'
    else if (last.kind === 'declined') outcome = 'declined'
    incidents.push({
      subject,
      events: chain,
      opened: chain[0].at,
      closed: outcome === 'watching' ? null : last.at,
      outcome,
    })
  }
  // Newest incidents appear first for operator triage.
  incidents.sort((a, b) => (a.opened < b.opened ? 1 : -1))
  return { incidents, quiet }
}

function StepDetail({ event, zh, tx }: { event: TimelineEvent; zh: boolean; tx: ReturnType<typeof T> }) {
  const radius = event.blast_radius
  return (
    <div className="sx-detail">
      {event.summary ? <p className="sx-summary">{event.summary}</p> : null}
      {event.note ? <p className="sx-note">{event.note}</p> : null}
      {event.reason ? <p className="sx-note">{event.reason}</p> : null}
      {event.detail ? <p className="sx-note">{event.detail}</p> : null}

      {typeof event.streak === 'number' && event.need ? (
        <p className="sx-kv"><span>{zh ? '连续命中' : 'STREAK'}</span>{event.streak} / {event.need}</p>
      ) : null}
      {typeof event.remaining_sec === 'number' ? (
        <p className="sx-kv"><span>{zh ? '剩余冷却' : 'COOLDOWN LEFT'}</span>{event.remaining_sec}s</p>
      ) : null}
      {typeof event.samples === 'number' && event.samples > 0 ? (
        <p className="sx-kv"><span>{tx.samples}</span>{event.samples}</p>
      ) : null}

      {radius ? (
        <div className={`sx-radius scope-${radius.scope}`}>
          <span className="sx-radius-k">{tx.radius} · {radius.scope}</span>
          <span>{radius.summary}</span>
        </div>
      ) : null}

      {event.baseline ? (
        <div className="sx-baseline">
          <span className="sx-kv-k">{tx.baseline}</span>
          {Object.entries(event.baseline).map(([probe, healthy]) => (
            <em key={probe} className={healthy ? 'is-up' : 'is-down'}>
              {probe} {healthy ? (zh ? '正常' : 'ok') : (zh ? '异常' : 'bad')}
            </em>
          ))}
        </div>
      ) : null}

      {event.error ? <pre className="sx-err">{event.error}</pre> : null}
    </div>
  )
}

export function SentinelTimeline({ lang, focus }: { lang: Lang; focus?: string }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [live, setLive] = useState(true)
  const [polling, setPolling] = useState(false)
  const [raw, setRaw] = useState(false)
  const [updated, setUpdated] = useState<string>('')
  const inFlight = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    inFlight.current?.abort()
    const controller = new AbortController()
    inFlight.current = controller
    try {
      const response = await fetch('/api/rca/sentinel/timeline?limit=400', { signal: controller.signal })
      const body = (await response.json()) as { ok?: boolean; events?: TimelineEvent[] }
      if (!response.ok || !body.ok) throw new Error(`gateway ${response.status}`)
      setSt({ s: 'ok', events: body.events ?? [] })
      setUpdated(new Date().toLocaleTimeString())
    } catch (error) {
      if ((error as Error).name === 'AbortError') return
      setSt({ s: 'err', m: error instanceof Error ? error.message : String(error) })
    } finally {
      if (inFlight.current === controller) inFlight.current = null
    }
  }, [])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => { if (!inFlight.current) void load() }, 5000)
    return () => window.clearInterval(timer)
  }, [live, load])

  useEffect(() => () => inFlight.current?.abort(), [])

  const pollNow = useCallback(async () => {
    setPolling(true)
    try {
      await fetch('/api/rca/sentinel/poll', { method: 'POST' })
    } catch {
      // The timeline refresh captures polls that complete after the request window.
    } finally {
      setPolling(false)
      void load()
    }
  }, [load])

  const { incidents: all, quiet } = useMemo(
    () => (st.s === 'ok' ? group(st.events) : { incidents: [], quiet: 0 }),
    [st],
  )
  // A selected address on the ledger above narrows this to that address's own
  // chain; with nothing selected the whole history shows.
  const incidents = useMemo(
    () => (focus ? all.filter((i) => i.subject.includes(focus)) : all),
    [all, focus],
  )

  return (
    <div className="sx">
      <header className="sx-head">
        <div>
          <p className="sx-lede">{tx.lede}</p>
        </div>
        <div className="sx-controls">
          <button type="button" className="sx-btn is-primary" onClick={() => void pollNow()} disabled={polling}>
            {polling ? tx.polling : tx.poll}
          </button>
          <button type="button" className="sx-btn" onClick={() => void load()}>{tx.refresh}</button>
          <label className="sx-live">
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
            {tx.live}
          </label>
          {updated ? <span className="sx-updated">{tx.updated} {updated}</span> : null}
        </div>
      </header>

      {st.s === 'load' ? <div className="sx-state">{tx.loading}</div> : null}
      {st.s === 'err' ? <div className="sx-state is-err">{st.m}</div> : null}

      {st.s === 'ok' && !incidents.length ? (
        <div className="sx-state">
          {focus
            ? (zh ? `${focus} 当前没有处置审计记录。` : `No response audit records for ${focus}.`)
            : tx.empty}
        </div>
      ) : null}

      {st.s === 'ok' && incidents.length ? (
        <>
          {focus ? (
            <p className="sx-quiet">
              {zh ? `只显示与 ${focus} 相关的处置链，共 ${incidents.length} 条` : `Filtered to ${focus}: ${incidents.length}`}
            </p>
          ) : null}
          {quiet ? <p className="sx-quiet">{quiet} {tx.quiet}</p> : null}
          <ol className="sx-incidents">
            {incidents.map((incident) => (
              <li className={`sx-incident out-${incident.outcome}`} key={`${incident.subject}-${incident.opened}`}>
                <div className="sx-inc-head">
                  <span className="sx-inc-subject">{incident.subject}</span>
                  <span className="sx-inc-outcome">{pick(OUTCOME_LABEL[incident.outcome], zh)}</span>
                  <span className="sx-inc-span">
                    {clock(incident.opened)}{incident.closed ? ` → ${clock(incident.closed)}` : ' …'}
                  </span>
                </div>
                <ol className="sx-steps">
                  {incident.events.map((event, index) => (
                    <li className={`sx-step k-${event.kind}`} key={`${event.at}-${index}`}>
                      <span className="sx-time">{clock(event.at)}</span>
                      <span className="sx-step-k">{pick(STEP_LABEL[event.kind] ?? [event.kind, event.kind], zh)}</span>
                      <StepDetail event={event} zh={zh} tx={tx} />
                    </li>
                  ))}
                </ol>
              </li>
            ))}
          </ol>

          <button type="button" className="sx-btn sx-raw-toggle" onClick={() => setRaw((v) => !v)} aria-expanded={raw}>
            {raw ? tx.hideRaw : tx.showRaw}
          </button>
          {raw ? (
            <pre className="sx-raw">
              {st.events.map((e) => `${clock(e.at)}  ${e.kind}  ${e.subject ?? ''}`).join('\n')}
            </pre>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
