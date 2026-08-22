import { useEffect, useRef, useState } from 'react'

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
}

/** Events that belong to a subject's chain; the rest is loop bookkeeping. */
export const SUBJECT_KINDS = new Set([
  'detected', 'awaiting_confirmation', 'no_safe_action', 'cooldown',
  'preflight', 'declined', 'remediated', 'resolved',
])

const TERMINAL_KINDS = new Set(['resolved', 'remediated', 'no_safe_action', 'declined', 'cooldown'])
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
export function useSentinelChain(subject: string | null | undefined, intervalMs = 3000): ChainStep[] | null {
  const [steps, setSteps] = useState<ChainStep[] | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    if (!subject) { setSteps(null); return }
    let alive = true
    let timer: number | undefined
    let settled = 0

    const load = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        const response = await fetch('/api/rca/sentinel/timeline?limit=400')
        const body = (await response.json()) as { events?: ChainStep[] }
        if (!alive) return
        const mine = (body.events ?? []).filter(
          (e) => SUBJECT_KINDS.has(e.kind) && (e.subject ?? '').includes(subject),
        )
        setSteps(mine)
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

  return steps
}
