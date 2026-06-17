// Shape of /api/rca/snapshot — served from the real network_rca framework.

export interface Evidence {
  evidenceId: string
  source: string
  summary: string
}

export interface Diagnosis {
  rootCauseKey: string
  rootCause: string
  confidence: number
  readonly: boolean
  evidence: Evidence[]
  recommendedActions: string[]
}

export interface TraceEvent {
  kind: string
  payload: Record<string, unknown>
}

export interface VerifierResult {
  passed: boolean
  errors: string[]
}

export interface RcaCase {
  id: string
  title: string
  query: string
  assets: string[]
  diagnosis: Diagnosis
  verifier: VerifierResult
  trace: TraceEvent[]
}

export interface Baseline {
  name: string
  rootCauseAccuracy: number
  evidenceRecall: number
  verifierPassRate: number
  cases: number
  notes: string
}

export interface DataStats {
  source: string
  windowDays: string[]
  adminLoginFailed: number
  distinctSrc: number
  topAttackerSrc: [string, number][]
  lockouts: number
  denyCount: number
  topDenyPorts: [string, number][]
  topDenySrc: [string, number][]
  acceptPermit: number
  sessionClash: number
}

export interface Readiness {
  blocked: boolean
  reason: string
  syslogPortOpen: boolean
  manifestValid: boolean
}

export interface RcaSnapshot {
  readiness: Readiness
  datasetReady: boolean
  dataStats: DataStats | null
  cases: RcaCase[]
  baselines: Baseline[]
  note: string
}
