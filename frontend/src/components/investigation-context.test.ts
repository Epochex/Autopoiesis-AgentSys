import { describe, expect, it } from 'vitest'
import {
  adjacentIntent,
  createInvestigationContext,
  investigationView,
  parseInvestigationContext,
  visitInvestigationContext,
} from './investigation-context'
import { actionLabel, humanizeIncidentSummary, humanizeMemoryRecord } from './incident-copy'

describe('persistent investigation context', () => {
  it('carries the source event and a bounded event window into investigation', () => {
    const context = createInvestigationContext({
      caseId: 'case-17',
      subject: '192.168.1.17',
      sourceEvent: { id: 'event-17', observedAt: '2026-09-07T10:00:00Z', summary: 'owner changed' },
      originView: 'overview',
      now: '2026-09-07T10:01:00Z',
    })

    expect(context).toMatchObject({
      caseId: 'case-17', subject: '192.168.1.17', currentIntent: 'event_facts',
      sourceEvent: { id: 'event-17', observedAt: '2026-09-07T10:00:00.000Z' },
      timeWindow: { start: '2026-09-07T09:55:00.000Z', end: '2026-09-07T10:05:00.000Z' },
    })
    expect(parseInvestigationContext(JSON.stringify(context))).toEqual(context)
  })

  it('records ordered visits and maps memory to the online-memory page', () => {
    const started = createInvestigationContext({ subject: 'gateway', originView: 'console', now: '2026-09-07T10:00:00Z' })
    const action = visitInvestigationContext(started, 'remediation_readback', undefined, '2026-09-07T10:01:00Z')
    const memory = visitInvestigationContext(action, 'related_memory', undefined, '2026-09-07T10:02:00Z')

    expect(action.visitedViews.at(-1)?.view).toBe('diagnose')
    expect(memory.visitedViews.at(-1)?.view).toBe('trajectory')
    expect(investigationView('impact_scope')).toBe('console')
    expect(adjacentIntent('evidence_hypotheses', -1)).toBeNull()
    expect(investigationView('evidence_hypotheses')).toBe('investigation')
    expect(investigationView('retrieval_evidence')).toBe('retrieval')
    expect(adjacentIntent('impact_scope', 1)).toBe('retrieval_evidence')
    expect(parseInvestigationContext(JSON.stringify({ ...started, currentIntent: 'evidence_hypotheses' }))?.currentIntent).toBe('event_facts')
    expect(adjacentIntent('related_memory', 1)).toBeNull()
  })

  it('rejects polluted persisted values', () => {
    expect(parseInvestigationContext('{"version":1,"subject":""}')).toBeNull()
    expect(parseInvestigationContext('not-json')).toBeNull()
  })
})

describe('incident-facing copy', () => {
  it('renders measured L2 ownership changes as a Chinese fact sentence', () => {
    expect(humanizeIncidentSummary(
      '192.168.1.110 changed L2 owner 1442x across 9451 captures, between 4 MACs',
      { measured: { captures: 9451, handovers: 1442, macs: ['a', 'b', 'c', 'd'] } },
      true,
      '192.168.1.110',
    )).toBe('192.168.1.110 在 9,451 次采集中出现 1,442 次 MAC 归属变化，涉及 4 个 MAC。')
  })

  it('states the operator action when an escalated case has no action text', () => {
    expect(actionLabel({ state: 'escalated' }, true)).toBe('转人工确认归属并制定变更')
  })

  it('turns a stored procedure record into an operator-facing finding', () => {
    expect(humanizeMemoryRecord(
      'verified duplicate_ip_static; successful checks: adapter:environment_finding',
      ['root:duplicate_ip_static', 'probe:adapter:environment_finding'],
      ['192.168.1.110'],
      true,
    )).toBe('已确认 192.168.1.110 的地址当前归属与登记信息冲突，证据来自环境观测。')
  })
})
