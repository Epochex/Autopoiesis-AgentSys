import { describe, expect, it } from 'vitest'
import { prepareIncidentList } from './incident-list'

describe('incident centre list', () => {
  it('keeps the newest row for a repeated active incident and sorts by observation time', () => {
    const rows = prepareIncidentList([
      { caseId: 'case-old', subject: 'same-host', ruleId: 'failed_units', status: 'escalated', lastSeenAt: '2026-09-10T10:00:00Z' },
      { caseId: 'case-new', subject: 'same-host', ruleId: 'failed_units', status: 'escalated', lastSeenAt: '2026-09-10T10:00:02Z' },
      { caseId: 'case-demo', subject: 'demo-host', ruleId: 'failed_units', status: 'investigating', lastSeenAt: '2026-09-10T10:00:03Z', controlled: true },
    ])

    expect(rows.map(row => row.caseId)).toEqual(['case-demo', 'case-new'])
  })

  it('shows one controlled comparison run after both internal cases close', () => {
    const rows = prepareIncidentList([
      { caseId: 'comparison', subject: 'demo-host', ruleId: 'failed_units', status: 'resolved', lastSeenAt: '2026-09-10T10:00:02Z', controlled: true },
      { caseId: 'primary', subject: 'demo-host', ruleId: 'failed_units', status: 'resolved', lastSeenAt: '2026-09-10T10:00:03Z', controlled: true },
    ])
    expect(rows.map(row => row.caseId)).toEqual(['primary'])
  })
})
