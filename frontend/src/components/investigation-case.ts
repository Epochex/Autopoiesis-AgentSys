import { useCallback, useEffect, useState } from 'react'

export interface CaseEvidence {
  evidenceId: string
  label: string
  value: string
  source: string
  observedAt?: string
}

export interface CaseBusinessDecision {
  caseId: string
  sessionId: string
  state: 'investigating' | 'action_ready' | 'observing' | 'resolved' | 'escalated'
  classification: string
  headline: string
  summary: string
  disposition: string
  action: string
  service?: string
  impactedAssets: string[]
  evidence: CaseEvidence[]
  missingObservations: { code: string; question: string; probe?: string | null }[]
  nextProbe?: string | null
  readback?: Record<string, unknown> | null
  generatedAt?: string
}

export interface InvestigationCaseRecord {
  caseId: string
  status: string
  severity: string
  subject: string
  service: string
  ruleId: string
  scope?: string
  summary: string
  firstSeenAt: string
  lastSeenAt: string
  updatedAt: string
  occurrenceCount: number
  timeline: Record<string, unknown>[]
  businessDecision?: CaseBusinessDecision | null
  sourcePayload?: Record<string, unknown>
}

interface CaseResponse {
  case?: InvestigationCaseRecord
}

/** One shared live read for every case surface. */
export function useInvestigationCase(caseId?: string, intervalMs = 8_000) {
  const [record, setRecord] = useState<InvestigationCaseRecord | null>(null)
  const [error, setError] = useState<{ caseId: string; message: string } | null>(null)
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])

  useEffect(() => {
    if (!caseId) {
      setRecord(null)
      setError(null)
      return
    }
    let alive = true
    let inFlight = false
    const load = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const response = await fetch(`/api/rca/investigation-cases/${encodeURIComponent(caseId)}`, {
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json() as CaseResponse | InvestigationCaseRecord
        if (!alive) return
        const next = 'case' in data && data.case ? data.case : data as InvestigationCaseRecord
        if (next.caseId !== caseId) throw new Error('Case identity mismatch')
        setRecord(next)
        setError(null)
      } catch (caught) {
        if (alive) setError({ caseId, message: caught instanceof Error ? caught.message : String(caught) })
      } finally {
        inFlight = false
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), intervalMs)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [caseId, intervalMs, revision])

  return { record: record?.caseId === caseId ? record : null, error: error?.caseId === caseId ? error?.message ?? null : null, refresh }
}
