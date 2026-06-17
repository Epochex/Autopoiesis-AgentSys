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
export interface Provider {
  id: string
  label: string
  kind: string
  model: string
  reachable: boolean
  note: string
}
export interface Readiness {
  blocked: boolean
  reason: string
  syslogPortOpen: boolean
  manifestValid: boolean
}
export interface Interface {
  name: string
  role: string
  flows: number
  kind: string
}
export interface Device {
  ip: string
  flows: number
  deny: number
  accept: number
  threat: 'high' | 'watch' | 'ok'
  top_ports: string[]
}
export interface Subnet {
  cidr: string
  hosts: number
  flows: number
  accept: number
  intf: string
  devices?: Device[]
}
export interface Anchor {
  ip: string
  name: string
  role: string
  intf: string
}
export interface Topology {
  core: { name: string; ip: string; model: string }
  interfaces: Interface[]
  subnets: Subnet[]
  anchors: Anchor[]
}
export interface MeshNode {
  ip: string
  out: number
  deny: number
  accept: number
  ports: string[]
  role: string
  threat: 'high' | 'watch' | 'ok'
}
export interface RcaSnapshot {
  readiness: Readiness
  datasetReady: boolean
  provider: string
  reasonerMode: string
  providers: Provider[]
  providerError: string | null
  topology: Topology | null
  meshes: Record<string, MeshNode[]>
  dataStats: DataStats | null
  cases: RcaCase[]
  baselines: Baseline[]
  note: string
}
