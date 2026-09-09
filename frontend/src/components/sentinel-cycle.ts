/**
 * Return the current incident cycle for one sentinel subject.
 *
 * The append-only ledger deliberately keeps earlier successful repairs.  A new
 * detection after a measured recovery opens a new cycle; without this boundary,
 * an old `resolved` paints the newly failed service as healthy. Safety-gated,
 * cooling and escalated decisions stay in one cycle while detections continue.
 */
export function latestIncidentCycle<T extends { kind?: unknown; needs_human?: unknown }>(events: T[]): T[] {
  let start = 0
  let nextDetectionStartsCycle = false

  events.forEach((event, index) => {
    const kind = String(event.kind ?? '')
    if (kind === 'detected' && nextDetectionStartsCycle) {
      start = index
      nextDetectionStartsCycle = false
    }

    if (
      kind === 'resolved'
      || kind === 'condition_cleared'
      || kind === 'escalation_cleared'
    ) {
      nextDetectionStartsCycle = true
    }
  })

  return events.slice(start)
}

/** Match an exact device/service key before selecting its latest cycle. */
export function subjectIncidentCycle<T extends { subject?: string; kind?: unknown }>(events: T[], subject: string): T[] {
  return latestIncidentCycle(events.filter(event => event.subject === subject))
}
