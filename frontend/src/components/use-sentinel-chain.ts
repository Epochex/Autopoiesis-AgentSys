import { useEffect, useRef, useState } from 'react'
import type { PriorCycle } from '../types'

/* ── one live read of a sentinel chain, shared by everything that draws it ────
 *
 * The theater used to freeze: it computed which steps had run at the moment it
 * opened and never looked again, so an incident could walk its whole six-step
 * chain while the stage sat still. Only the corner progress panel polled, which
 * is why the panel was the only thing that ever moved.
 *
 * Both surfaces now read the same live chain from here, so the rail, the marker
 * on the topology and the progress rail advance together. */

export interface ChainStep {
  at: string
  kind: string
  subject?: string
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
  /* Carried only by `escalated`: how many times this exact fix has already
     worked and been undone, over what window, and the rounds themselves. */
  recurrences?: number
  window_hours?: number
  prior_cycles?: PriorCycle[]
}

/** Events that belong to a subject's chain; the rest is loop bookkeeping. */
export const SUBJECT_KINDS = new Set([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved', 'escalated',
])

/** One shell command the sentinel ran, and what came back. */
export interface CommandLine {
  at: string
  argv: string[]
  rc: number
  out: string
  truncated?: boolean
}

export interface Chain {
  /** The graded steps, in order — what the rail and the phase labels read. */
  steps: ChainStep[]
  /** The transcript, in order — every command, streamed as it ran. */
  commands: CommandLine[]
}

const TERMINAL_KINDS = new Set([
  'resolved', 'remediated', 'no_safe_action', 'declined', 'cooldown', 'escalated',
])
/** Keep reading briefly past the first terminal step — see the note on the hook. */
const GRACE_POLLS = 3

/**
 * Poll one subject's chain.
 *
 * `intervalMs` is 3s by default: the watch window is ninety seconds of nothing
 * visibly happening, which is exactly when a slow poll reads as a hang.
 *
 * Polling winds down once the chain closes — a closed incident does not change
 * again — but not on the first sighting of a terminal step. `remediated` and
 * `resolved` are two separate appends, so a read landing between them would
 * otherwise freeze the stage at five of six steps forever. A couple of grace
 * polls costs nothing and removes that race.
 */
export function useSentinelChain(subject: string | null | undefined, intervalMs = 3000): Chain | null {
  const [chain, setChain] = useState<Chain | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    if (!subject) { setChain(null); return }
    let alive = true
    let timer: number | undefined
    let settled = 0

    const load = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        // 800: the transcript is one line per command, and a watch window alone
        // adds two dozen. A short tail would drop the start of the chain.
        const response = await fetch('/api/rca/sentinel/timeline?limit=800')
        const body = (await response.json()) as { events?: (ChainStep & CommandLine)[] }
        if (!alive) return
        const ours = (body.events ?? []).filter((e) => (e.subject ?? '').includes(subject))
        const mine = ours.filter((e) => SUBJECT_KINDS.has(e.kind))
        setChain({
          steps: mine,
          commands: ours.filter((e) => e.kind === 'command') as unknown as CommandLine[],
        })
        if (mine.some((e) => TERMINAL_KINDS.has(e.kind))) {
          settled += 1
          if (settled >= GRACE_POLLS && timer) {
            window.clearInterval(timer)
            timer = undefined
          }
        } else {
          settled = 0
        }
      } catch {
        // A blip must not blank a stage the operator is reading; keep the last view.
      } finally {
        inFlight.current = false
      }
    }

    void load()
    timer = window.setInterval(() => void load(), intervalMs)
    return () => { alive = false; if (timer) window.clearInterval(timer) }
  }, [subject, intervalMs])

  return chain
}
