import { describe, expect, it } from 'vitest'
import { caseNetworkContext, caseSessionIds, isSessionForCase, type CaseSession } from './case-investigation-data'
import type { InvestigationCaseRecord } from './investigation-case'

describe('case investigation identity', () => {
  it('accepts a serial-number case mapped to the managed gateway by the exact case link', () => {
    const session = { case_id: 'firewall-case', session_id: 'auth-session', subject: '192.168.1.1' } as CaseSession
    expect(isSessionForCase(session, 'firewall-case', 'auth-session')).toBe(true)
    expect(isSessionForCase(session, 'address-conflict-case', 'auth-session')).toBe(false)
    expect(isSessionForCase(session, 'firewall-case', 'other-session')).toBe(false)
  })
  it('prefers the decision session and includes only directly linked case sessions', () => {
    const record = { businessDecision: { sessionId: 'decisive' }, timeline: [
      { kind: 'investigation_session_started', sessionId: 'initial' },
      { kind: 'investigation_session_started', sessionId: 'decisive' },
      { kind: 'evidence_collected', sessionId: 'unrelated' },
      { kind: 'investigation_pair_measured', report: { session_id: 'evaluation' } },
    ] } as unknown as InvestigationCaseRecord
    expect(caseSessionIds(record)).toEqual(['decisive', 'initial'])
  })
  it('reads only a saved network-context receipt and preserves unavailable context', () => {
    const session = { evidence: [{ command: 'fortigate:network_context 192.168.1.1', output: '{"managedDeviceIdentity":{"serial":"FG100ETK20014183"}}' }] } as CaseSession
    expect(caseNetworkContext(session).context).toEqual({ managedDeviceIdentity: { serial: 'FG100ETK20014183' } })
    expect(caseNetworkContext(null).context).toEqual({})
  })
})
