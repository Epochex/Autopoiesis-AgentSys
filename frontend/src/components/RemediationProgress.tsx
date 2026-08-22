import { useEffect, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'

/* ── 处置进度 — what the system is doing to this device, right now ───────────
 *
 * Lives inside the theater so the blast radius and the response are read in one
 * place: the topology says which nodes are involved, this says what is being
 * done to them and how far along it is.
 *
 * The watch window is the reason this exists as live progress rather than a
 * log. Ninety seconds of "nothing visible is happening" is exactly when an
 * operator assumes the thing has hung — so the sample count ticks up while it
 * waits, and the bar says what it is waiting for. */

interface Step {
  at: string
  kind: string
  action?: string | null
  eligible?: boolean
  reason?: string
  outcome?: string
  samples?: number
  streak?: number
  need?: number
  note?: string
  summary?: string
  needs_human?: boolean
  blast_radius?: { scope: string; summary: string } | null
  baseline?: Record<string, boolean> | null
}

/** The chain's shape, in the order the system walks it. */
const PHASES = ['detected', 'confirmed', 'preflight', 'acting', 'watching', 'closed'] as const
type Phase = (typeof PHASES)[number]

const PHASE_LABEL: Record<Phase, [string, string]> = {
  detected: ['发现', 'DETECTED'],
  confirmed: ['已确认', 'CONFIRMED'],
  preflight: ['前置校验', 'PREFLIGHT'],
  acting: ['执行', 'ACTING'],
  watching: ['观察期', 'WATCHING'],
  closed: ['收尾', 'CLOSED'],
}

function phaseOf(steps: Step[]): { reached: Set<Phase>; current: Phase; terminal: string | null } {
  const reached = new Set<Phase>()
  let terminal: string | null = null
  for (const step of steps) {
    switch (step.kind) {
      case 'detected':
        reached.add('detected')
        if ((step.streak ?? 0) >= (step.need ?? 2)) reached.add('confirmed')
        break
      case 'preflight':
        reached.add('confirmed')
        reached.add('preflight')
        break
      case 'remediated':
        reached.add('acting')
        reached.add('watching')
        reached.add('closed')
        terminal = step.needs_human ? 'needs_human' : step.outcome ?? null
        break
      case 'resolved':
        reached.add('closed')
        terminal = 'resolved'
        break
      case 'no_safe_action':
        reached.add('confirmed')
        reached.add('closed')
        terminal = 'no_safe_action'
        break
      case 'declined':
      case 'cooldown':
        reached.add('closed')
        terminal = step.kind
        break
      default:
        break
    }
  }
  // A preflight that passed with no verdict yet means the action is in flight —
  // including the long silent stretch of the watch window.
  if (reached.has('preflight') && !reached.has('closed')) {
    reached.add('acting')
    reached.add('watching')
  }
  const current = [...PHASES].reverse().find((p) => reached.has(p)) ?? 'detected'
  return { reached, current, terminal }
}

const TERMINAL_LABEL: Record<string, [string, string]> = {
  resolved: ['已恢复', 'RESOLVED'],
  passed: ['已恢复', 'RESOLVED'],
  needs_human: ['需人工介入', 'NEEDS A PERSON'],
  no_safe_action: ['只报不动', 'REPORTED, NOT ACTED'],
  declined: ['前置条件不通过', 'PRECONDITIONS FAILED'],
  cooldown: ['冷却中', 'COOLING DOWN'],
  reverted: ['已回滚', 'REVERTED'],
  revert_unverified: ['回滚未能验证', 'REVERT UNVERIFIED'],
}

const SUBJECT_KINDS = new Set([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved',
])

export function RemediationProgress({ subject, lang }: { subject: string; lang: Lang }) {
  const zh = lang === 'zh'
  const [steps, setSteps] = useState<Step[] | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    if (!subject) return
    let alive = true
    const load = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        const response = await fetch('/api/rca/sentinel/timeline?limit=400')
        const body = (await response.json()) as { events?: (Step & { subject?: string })[] }
        if (!alive) return
        const mine = (body.events ?? []).filter(
          (e) => SUBJECT_KINDS.has(e.kind) && (e.subject ?? '').includes(subject),
        )
        setSteps(mine)
      } catch {
        if (alive) setSteps([])
      } finally {
        inFlight.current = false
      }
    }
    void load()
    // Fast poll: this panel is watched during the window, and the window is the
    // part that otherwise looks like a hang.
    const timer = window.setInterval(() => void load(), 3000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [subject])

  const view = useMemo(() => (steps ? phaseOf(steps) : null), [steps])

  if (!steps || !steps.length || !view) return null

  const last = steps[steps.length - 1]
  const samples = [...steps].reverse().find((s) => typeof s.samples === 'number')?.samples
  const radius = [...steps].reverse().find((s) => s.blast_radius)?.blast_radius
  const running = !view.reached.has('closed')

  return (
    <div className={`rp${running ? ' is-running' : ''}`}>
      <div className="rp-head">
        <span className="rp-k">{zh ? '系统正在处置' : 'SYSTEM RESPONSE'}</span>
        <span className="rp-subject">{subject}</span>
        {view.terminal ? (
          <span className={`rp-terminal t-${view.terminal}`}>
            {(TERMINAL_LABEL[view.terminal] ?? [view.terminal, view.terminal])[zh ? 0 : 1]}
          </span>
        ) : (
          <span className="rp-terminal t-running">{zh ? '进行中' : 'IN FLIGHT'}</span>
        )}
      </div>

      <ol className="rp-rail">
        {PHASES.map((phase) => {
          const done = view.reached.has(phase)
          const now = phase === view.current && running
          return (
            <li key={phase} className={`rp-phase${done ? ' is-done' : ''}${now ? ' is-now' : ''}`}>
              <span className="rp-dot" />
              <span className="rp-lab">{PHASE_LABEL[phase][zh ? 0 : 1]}</span>
              {phase === 'watching' && typeof samples === 'number' && samples > 0 ? (
                <span className="rp-count">{samples} {zh ? '次采样' : 'samples'}</span>
              ) : null}
            </li>
          )
        })}
      </ol>

      {radius ? (
        <p className="rp-line">
          <span>{zh ? '影响面' : 'BLAST RADIUS'}</span>{radius.summary}
        </p>
      ) : null}
      {last.note || last.reason || last.summary ? (
        <p className="rp-line"><span>{zh ? '当前' : 'NOW'}</span>{last.note || last.reason || last.summary}</p>
      ) : null}
      {running && view.current === 'watching' ? (
        <p className="rp-hint">
          {zh
            ? '改完了，但还没算修好。观察期里持续回读这台设备和网关，指标回退就自动退回上一状态。'
            : 'Changed, but not yet fixed. The window keeps re-reading this device and the gateway; a regression reverts it.'}
        </p>
      ) : null}
    </div>
  )
}
