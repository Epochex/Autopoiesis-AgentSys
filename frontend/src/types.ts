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
/* ── per-subnet device graph, mined from raw syslog (build_device_graph.py) ── */
export interface GraphDevice {
  ip: string
  name: string | null
  mac: string | null
  vendor: string
  os: string | null
  role: string
  intf: string | null
  flows: number
  deny: number
  accept: number
  leases: number
  topPorts: string[]
  threat: 'high' | 'watch' | 'ok'
  seenBy: 'traffic' | 'dhcp'
  x: number
  y: number
}
export type EdgeKind = 'clash' | 'bcast' | 'codst' | 'fleet' | 'family' | 'lease' | 'portfp'
export interface GraphEdge {
  src: string
  dst: string
  kind: EdgeKind
  weight: number
  hits: number
  evidence: string
  observed: boolean
}
export interface GraphCluster {
  id: string
  members: string[]
  role: string
  vendor: string
  size: number
  boundBy: EdgeKind[]
  deny: number
}
export interface GraphAnomaly {
  kind: string
  members: string[]
  detail: string
}
export interface SubnetGraph {
  cidr: string
  devices: GraphDevice[]
  edges: GraphEdge[]
  clusters: GraphCluster[]
  anomalies: GraphAnomaly[]
  stats: {
    devices: number
    withTraffic: number
    dhcpOnly: number
    edges: number
    observedEdges: number
    deny: number
    roles: Record<string, number>
    vendors: Record<string, number>
  }
}
export interface GraphPattern {
  title: string
  kind: string
  members: string[]
  why: string
  severity: 'high' | 'medium' | 'low'
  confidence?: number
}
export interface GraphAnalysis {
  cidr: string
  loading?: boolean
  error?: string
  summary?: string
  communities?: { id: string; label: string; note: string }[]
  patterns?: GraphPattern[]
  corridors?: { src: string; dst: string; why: string }[]
  flow?: string
  blindSpot?: string
  actions?: string[]
  model?: string
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
