import { useEffect, useState } from 'react'
import type { PriorCycle } from '../types'
import { subjectIncidentCycle } from './sentinel-cycle'

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
  reading?: Record<string, unknown>
  healthy?: boolean
  available?: boolean
  at: string
  kind: string
  subject?: string
  action?: string | null
  eligible?: boolean
  reason?: string
  outcome?: string
  samples?: number
  target_recovered?: boolean | null
  phase?: string
  streak?: number
  need?: number
  error?: string
  stage?: string
  note?: string
  summary?: string
  evidence?: Record<string, unknown>
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
  'escalation_cleared', 'condition_cleared', 'remediation_started', 'execution_error',
  // Follow-up is emitted while execute() is still blocking inside the watch
  // window. Keeping these existing ledger rows in `steps` lets the theater
  // advance ACT → WATCH → VERIFY on each poll instead of jumping from
  // PREFLIGHT straight to a closed result. No phase is inferred here: the
  // hook preserves the backend kinds exactly as written.
  'remediation_committed', 'bakein_opened', 'bakein_sampled',
  'bakein_passed', 'bakein_regressed', 'remediation_reverted',
  'revert_unverified',
])

/** One shell command the sentinel ran, and what came back. */
export interface CommandLine {
  at: string
  argv: string[]
  rc: number | null
  command_id?: string
  receipt?: Record<string, unknown>
  source?: string
  change_id?: string
  phase?: 'started' | 'completed' | 'error' | 'receipt'
  stdout?: string | null
  stderr?: string | null
  error?: string
  started_at?: string
  finished_at?: string
  duration_ms?: number
  purpose?: string
  verification?: boolean
  stage?: string
  out: string
  truncated?: boolean
}

export interface Chain {
  /** The graded steps, in order — what the rail and the phase labels read. */
  steps: ChainStep[]
  /** The transcript, in order — every command, streamed as it ran. */
  commands: CommandLine[]
  lastReadAt?: string
  readError?: string
}

/** Keep polling the selected subject while this view is mounted. A refusal or
 * remediation receipt can be followed by a later verification or new cycle. */
export function useSentinelChain(subject: string | null | undefined, intervalMs = 3000): Chain | null {
  const [chain, setChain] = useState<Chain | null>(null)

  useEffect(() => {
    if (!subject) { setChain(null); return }
    let alive = true
    let inFlight = false
    const abort = new AbortController()

    const load = async () => {
      if (inFlight) return
      inFlight = true
      try {
        // Read the full retained tail, including command start and return rows.
        const response = await fetch('/api/rca/sentinel/timeline?limit=2000', { signal: abort.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const body = (await response.json()) as { events?: (ChainStep & CommandLine)[] }
        if (!alive) return
        const ours = subjectIncidentCycle(body.events ?? [], subject)
        const mine = ours.filter((e) => SUBJECT_KINDS.has(e.kind))
        setChain({
          steps: mine,
          lastReadAt: new Date().toISOString(),
          commands: ours.filter((e) => e.kind === 'command') as unknown as CommandLine[],
        })
      } catch (error) {
        if (alive) setChain(previous => previous ? {...previous, readError: error instanceof Error ? error.message : String(error)} : {steps: [], commands: [], readError: String(error)})
      } finally {
        inFlight = false
      }
    }

    void load()
    const timer = window.setInterval(() => void load(), intervalMs)
    return () => { alive = false; abort.abort(); if (timer) window.clearInterval(timer) }
  }, [subject, intervalMs])

  return chain
}
