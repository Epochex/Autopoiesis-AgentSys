/**
 * Return the current incident cycle for one sentinel subject.
 *
 * The append-only ledger deliberately keeps earlier successful repairs.  A new
 * detection after a closed decision opens a new cycle; without this boundary,
 * an old `resolved` paints the newly failed service as healthy.  Escalation is
 * intentionally sticky while detections continue because those detections are
 * the same refused incident, not fresh attempts.
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
      || kind === 'no_safe_action'
      || kind === 'declined'
      || kind === 'cooldown'
      || kind === 'escalation_cleared'
      || (kind === 'remediated' && event.needs_human === true)
    ) {
      nextDetectionStartsCycle = true
    }
  })

  return events.slice(start)
}
