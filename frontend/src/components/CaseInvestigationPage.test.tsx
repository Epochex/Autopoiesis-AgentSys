import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { CaseInvestigationPage } from './CaseInvestigationPage'
import type { InvestigationCaseRecord } from './investigation-case'

const state = vi.hoisted(() => ({ record: null as InvestigationCaseRecord | null, error: null as string | null }))
vi.mock('./investigation-case', () => ({ useInvestigationCase: () => ({ ...state, refresh() {} }) }))
vi.mock('./use-sentinel-chain', () => ({ useSentinelChain: () => null }))

describe('case investigation page', () => {
  it('shows persisted facts and decisive evidence even while the session detail is unavailable', () => {
    state.record = {
      caseId: 'firewall-case', subject: 'FG100ETK20014183', status: 'escalated', severity: 'critical', service: 'fortigate-admin', ruleId: 'admin_auth_attack_v1', summary: 'Authentication attack',
      firstSeenAt: '2026-09-08T06:57:25Z', lastSeenAt: '2026-09-08T06:57:25Z', updatedAt: '2026-09-08T06:58:08Z', occurrenceCount: 1, timeline: [],
      sourcePayload: { incidentFacts: { failedLogins: 12, distinctSources: 12, lockouts: 0, windowSeconds: 60 } },
      businessDecision: { caseId: 'firewall-case', sessionId: 'auth-session', state: 'escalated', classification: 'admin_bruteforce_lockout', headline: 'Confirmed distributed login attack', summary: '13 failed logins from 13 sources', disposition: 'Review permitted administration sources', action: '', impactedAssets: ['192.168.1.1'], evidence: [{ evidenceId: 'ev-007', label: '认证窗口', value: '已存档认证证据', source: 'live_tool' }], missingObservations: [] },
    }
    const html = renderToStaticMarkup(<CaseInvestigationPage lang="en" caseId="firewall-case" onBack={() => {}} />)
    expect(html).toContain('Confirmed distributed login attack')
    expect(html).toContain('FG100ETK20014183')
    expect(html).toContain('192.168.1.1')
    expect(html).toContain('Detect anomaly')
    expect(html).toContain('Confirm continuously')
    expect(html).toContain('Apply memory')
    expect(html).toContain('Verify continuously')
    expect(html).toContain('Technical audit record')
    expect(html).not.toContain('<details open')
    expect(html).not.toContain('192.168.1.110')
  })
  it('names a missing case instead of silently showing another investigation', () => {
    state.record = null
    state.error = 'HTTP 404'
    const html = renderToStaticMarkup(<CaseInvestigationPage lang="en" caseId="missing-case" />)
    expect(html).toContain('Incident unavailable')
    expect(html).toContain('HTTP 404')
    expect(html).not.toContain('Confirmed distributed login attack')
  })
})
