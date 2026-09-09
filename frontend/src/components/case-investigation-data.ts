import type { InvestigationCaseRecord } from './investigation-case'

export type CaseSession = {
  session_id: string
  case_id: string
  subject: string
  asset_ids?: string[]
  external_actors?: string[]
  incident_start?: string
  incident_end?: string
  scope_basis?: string[]
  scope_missing?: string[]
  evidence?: { evidence_id: string; command: string; output: string; ok: boolean; observed_at?: string; source?: string }[]
  working_set?: Record<string, unknown>
  hypothesis_state?: Record<string, unknown>
  trace_events?: Record<string, unknown>[]
  runbook?: Record<string, unknown>[]
  historical_context?: Record<string, unknown>
  knowledge_context?: Record<string, unknown>[]
  [key: string]: unknown
}

/** The case link remains authoritative when a serial number resolves to an IP. */
export function isSessionForCase(session: CaseSession, caseId: string, sessionId: string): boolean {
  return session.case_id === caseId && session.session_id === sessionId
}

export function caseSessionIds(record: InvestigationCaseRecord): string[] {
  const ids = record.timeline
    .filter(event => event.kind === 'investigation_session_started' && typeof event.sessionId === 'string')
    .map(event => String(event.sessionId)).reverse()
  const decisionSession = record.businessDecision?.sessionId
  return [...new Set([...(decisionSession ? [decisionSession] : []), ...ids])]
}

export function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function parsedOutput(value: string): Record<string, unknown> {
  try { return objectValue(JSON.parse(value)) } catch { return {} }
}

export function caseNetworkContext(session: CaseSession | null) {
  const evidence = session?.evidence?.find(item => item.command.startsWith('fortigate:network_context '))
  return { evidence, context: evidence ? parsedOutput(evidence.output) : {} }
}
