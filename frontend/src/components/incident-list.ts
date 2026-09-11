export interface IncidentListRow {
  caseId: string
  subject: string
  ruleId?: string
  status: string
  lastSeenAt: string
  controlled?: boolean
}

const ACTIVE = new Set(['open', 'investigating', 'waiting', 'escalated', 'action_ready', 'observing'])

/** One active row represents one object/rule processing cycle. */
export function prepareIncidentList<T extends IncidentListRow>(rows: T[]): T[] {
  const newest = [...rows].sort((a, b) => b.lastSeenAt.localeCompare(a.lastSeenAt))
  const activeKeys = new Set<string>()
  return newest.filter((row) => {
    if (!ACTIVE.has(row.status) && !row.controlled) return true
    const key = `${row.subject}\u0000${row.ruleId ?? ''}`
    if (activeKeys.has(key)) return false
    activeKeys.add(key)
    return true
  })
}
